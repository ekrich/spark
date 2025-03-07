#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from pyspark.sql.connect.utils import check_dependencies

check_dependencies(__name__)

import os
import warnings
from collections.abc import Sized
from distutils.version import LooseVersion
from functools import reduce
from threading import RLock
from typing import (
    Optional,
    Any,
    Union,
    Dict,
    List,
    Tuple,
    cast,
    overload,
    Iterable,
    TYPE_CHECKING,
)

import numpy as np
import pandas as pd
import pyarrow as pa
from pandas.api.types import (  # type: ignore[attr-defined]
    is_datetime64_dtype,
    is_datetime64tz_dtype,
    is_timedelta64_dtype,
)
import urllib

from pyspark import SparkContext, SparkConf, __version__
from pyspark.sql.connect.client import SparkConnectClient, ChannelBuilder
from pyspark.sql.connect.conf import RuntimeConf
from pyspark.sql.connect.dataframe import DataFrame
from pyspark.sql.connect.plan import SQL, Range, LocalRelation, CachedRelation
from pyspark.sql.connect.readwriter import DataFrameReader
from pyspark.sql.connect.streaming import DataStreamReader, StreamingQueryManager
from pyspark.sql.pandas.serializers import ArrowStreamPandasSerializer
from pyspark.sql.pandas.types import to_arrow_schema, to_arrow_type, _deduplicate_field_names
from pyspark.sql.session import classproperty, SparkSession as PySparkSession
from pyspark.sql.types import (
    _infer_schema,
    _has_nulltype,
    _merge_type,
    Row,
    DataType,
    DayTimeIntervalType,
    StructType,
    AtomicType,
    TimestampType,
)
from pyspark.sql.utils import to_str
from pyspark.errors import (
    PySparkAttributeError,
    PySparkNotImplementedError,
    PySparkRuntimeError,
    PySparkValueError,
    PySparkTypeError,
)

if TYPE_CHECKING:
    from pyspark.sql.connect._typing import OptionalPrimitiveType
    from pyspark.sql.connect.catalog import Catalog
    from pyspark.sql.connect.udf import UDFRegistration


# `_active_spark_session` stores the active spark connect session created by
# `SparkSession.builder.getOrCreate`. It is used by ML code.
_active_spark_session = None


class SparkSession:
    class Builder:
        """Builder for :class:`SparkSession`."""

        _lock = RLock()

        def __init__(self) -> None:
            self._options: Dict[str, Any] = {}
            self._channel_builder: Optional[ChannelBuilder] = None

        @overload
        def config(self, key: str, value: Any) -> "SparkSession.Builder":
            ...

        @overload
        def config(self, *, map: Dict[str, "OptionalPrimitiveType"]) -> "SparkSession.Builder":
            ...

        def config(
            self,
            key: Optional[str] = None,
            value: Optional[Any] = None,
            *,
            map: Optional[Dict[str, "OptionalPrimitiveType"]] = None,
        ) -> "SparkSession.Builder":
            with self._lock:
                if map is not None:
                    for k, v in map.items():
                        self._options[k] = to_str(v)
                else:
                    self._options[cast(str, key)] = to_str(value)
                return self

        def master(self, master: str) -> "SparkSession.Builder":
            return self

        def appName(self, name: str) -> "SparkSession.Builder":
            return self.config("spark.app.name", name)

        def remote(self, location: str = "sc://localhost") -> "SparkSession.Builder":
            return self.config("spark.remote", location)

        def channelBuilder(self, channelBuilder: ChannelBuilder) -> "SparkSession.Builder":
            """Uses custom :class:`ChannelBuilder` implementation, when there is a need
            to customize the behavior for creation of GRPC connections.

            .. versionadded:: 3.5.0

            An example to use this class looks like this:

            .. code-block:: python

                from pyspark.sql.connect import SparkSession, ChannelBuilder

                class CustomChannelBuilder(ChannelBuilder):
                    ...

                custom_channel_builder = CustomChannelBuilder(...)
                spark = SparkSession.builder().channelBuilder(custom_channel_builder).getOrCreate()

            Returns
            -------
            :class:`SparkSession.Builder`
            """
            with self._lock:
                # self._channel_builder is a separate field, because it may hold the state
                # and cannot be serialized with to_str()
                self._channel_builder = channelBuilder
                return self

        def enableHiveSupport(self) -> "SparkSession.Builder":
            raise PySparkNotImplementedError(
                error_class="NOT_IMPLEMENTED", message_parameters={"feature": "enableHiveSupport"}
            )

        def create(self) -> "SparkSession":
            has_channel_builder = self._channel_builder is not None
            has_spark_remote = "spark.remote" in self._options

            if has_channel_builder and has_spark_remote:
                raise ValueError(
                    "Only one of connection string or channelBuilder "
                    "can be used to create a new SparkSession."
                )

            if not has_channel_builder and not has_spark_remote:
                raise ValueError(
                    "Needs either connection string or channelBuilder to create a new SparkSession."
                )

            if has_channel_builder:
                assert self._channel_builder is not None
                return SparkSession(connection=self._channel_builder)
            else:
                spark_remote = to_str(self._options.get("spark.remote"))
                assert spark_remote is not None
                return SparkSession(connection=spark_remote)

        def getOrCreate(self) -> "SparkSession":
            global _active_spark_session
            if _active_spark_session is not None:
                return _active_spark_session
            _active_spark_session = self.create()
            return _active_spark_session

    _client: SparkConnectClient

    @classproperty
    def builder(cls) -> Builder:
        """Creates a :class:`Builder` for constructing a :class:`SparkSession`."""
        return cls.Builder()

    def __init__(self, connection: Union[str, ChannelBuilder], userId: Optional[str] = None):
        """
        Creates a new SparkSession for the Spark Connect interface.

        Parameters
        ----------
        connection: Union[str,ChannelBuilder]
            Connection string that is used to extract the connection parameters and configure
            the GRPC connection. Or instance of ChannelBuilder that creates GRPC connection.
            Defaults to `sc://localhost`.
        userId : str, optional
            Optional unique user ID that is used to differentiate multiple users and
            isolate their Spark Sessions. If the `user_id` is not set, will default to
            the $USER environment. Defining the user ID as part of the connection string
            takes precedence.
        """
        self._client = SparkConnectClient(connection=connection, userId=userId)
        self._session_id = self._client._session_id

    def table(self, tableName: str) -> DataFrame:
        return self.read.table(tableName)

    table.__doc__ = PySparkSession.table.__doc__

    @property
    def read(self) -> "DataFrameReader":
        return DataFrameReader(self)

    read.__doc__ = PySparkSession.read.__doc__

    @property
    def readStream(self) -> "DataStreamReader":
        return DataStreamReader(self)

    def _inferSchemaFromList(
        self, data: Iterable[Any], names: Optional[List[str]] = None
    ) -> StructType:
        """
        Infer schema from list of Row, dict, or tuple.
        """
        if not data:
            raise PySparkValueError(
                error_class="CANNOT_INFER_EMPTY_SCHEMA",
                message_parameters={},
            )

        (
            infer_dict_as_struct,
            infer_array_from_first_element,
            prefer_timestamp_ntz,
        ) = self._client.get_configs(
            "spark.sql.pyspark.inferNestedDictAsStruct.enabled",
            "spark.sql.pyspark.legacy.inferArrayTypeFromFirstElement.enabled",
            "spark.sql.timestampType",
        )
        return reduce(
            _merge_type,
            (
                _infer_schema(
                    row,
                    names,
                    infer_dict_as_struct=(infer_dict_as_struct == "true"),
                    infer_array_from_first_element=(infer_array_from_first_element == "true"),
                    prefer_timestamp_ntz=(prefer_timestamp_ntz == "TIMESTAMP_NTZ"),
                )
                for row in data
            ),
        )

    def createDataFrame(
        self,
        data: Union["pd.DataFrame", "np.ndarray", Iterable[Any]],
        schema: Optional[Union[AtomicType, StructType, str, List[str], Tuple[str, ...]]] = None,
    ) -> "DataFrame":
        assert data is not None
        if isinstance(data, DataFrame):
            raise PySparkTypeError(
                error_class="INVALID_TYPE",
                message_parameters={"arg_name": "data", "data_type": "DataFrame"},
            )

        _schema: Optional[Union[AtomicType, StructType]] = None
        _cols: Optional[List[str]] = None
        _num_cols: Optional[int] = None

        if isinstance(schema, str):
            schema = self.client._analyze(  # type: ignore[assignment]
                method="ddl_parse", ddl_string=schema
            ).parsed

        if isinstance(schema, (AtomicType, StructType)):
            _schema = schema
            if isinstance(schema, StructType):
                _num_cols = len(schema.fields)
            else:
                _num_cols = 1

        elif isinstance(schema, (list, tuple)):
            # Must re-encode any unicode strings to be consistent with StructField names
            _cols = [x.encode("utf-8") if not isinstance(x, str) else x for x in schema]
            _num_cols = len(_cols)

        if isinstance(data, np.ndarray) and data.ndim not in [1, 2]:
            raise PySparkValueError(
                error_class="INVALID_NDARRAY_DIMENSION",
                message_parameters={"dimensions": "1 or 2"},
            )
        elif isinstance(data, Sized) and len(data) == 0:
            if _schema is not None:
                return DataFrame.withPlan(LocalRelation(table=None, schema=_schema.json()), self)
            else:
                raise PySparkValueError(
                    error_class="CANNOT_INFER_EMPTY_SCHEMA",
                    message_parameters={},
                )

        _table: Optional[pa.Table] = None

        if isinstance(data, pd.DataFrame):
            # Logic was borrowed from `_create_from_pandas_with_arrow` in
            # `pyspark.sql.pandas.conversion.py`. Should ideally deduplicate the logics.

            # If no schema supplied by user then get the names of columns only
            if schema is None:
                _cols = [str(x) if not isinstance(x, str) else x for x in data.columns]
            elif isinstance(schema, (list, tuple)) and cast(int, _num_cols) < len(data.columns):
                assert isinstance(_cols, list)
                _cols.extend([f"_{i + 1}" for i in range(cast(int, _num_cols), len(data.columns))])
                _num_cols = len(_cols)

            # Determine arrow types to coerce data when creating batches
            arrow_schema: Optional[pa.Schema] = None
            spark_types: List[Optional[DataType]]
            arrow_types: List[Optional[pa.DataType]]
            if isinstance(schema, StructType):
                deduped_schema = cast(StructType, _deduplicate_field_names(schema))
                spark_types = [field.dataType for field in deduped_schema.fields]
                arrow_schema = to_arrow_schema(deduped_schema)
                arrow_types = [field.type for field in arrow_schema]
                _cols = [str(x) if not isinstance(x, str) else x for x in schema.fieldNames()]
            elif isinstance(schema, DataType):
                raise PySparkTypeError(
                    error_class="UNSUPPORTED_DATA_TYPE_FOR_ARROW",
                    message_parameters={"data_type": str(schema)},
                )
            else:
                # Any timestamps must be coerced to be compatible with Spark
                spark_types = [
                    TimestampType()
                    if is_datetime64_dtype(t) or is_datetime64tz_dtype(t)
                    else DayTimeIntervalType()
                    if is_timedelta64_dtype(t)
                    else None
                    for t in data.dtypes
                ]
                arrow_types = [to_arrow_type(dt) if dt is not None else None for dt in spark_types]

            timezone, safecheck = self._client.get_configs(
                "spark.sql.session.timeZone", "spark.sql.execution.pandas.convertToArrowArraySafely"
            )

            ser = ArrowStreamPandasSerializer(cast(str, timezone), safecheck == "true")

            _table = pa.Table.from_batches(
                [
                    ser._create_batch(
                        [
                            (c, at, st)
                            for (_, c), at, st in zip(data.items(), arrow_types, spark_types)
                        ]
                    )
                ]
            )

            if isinstance(schema, StructType):
                assert arrow_schema is not None
                _table = _table.rename_columns(
                    cast(StructType, _deduplicate_field_names(schema)).names
                ).cast(arrow_schema)

        elif isinstance(data, np.ndarray):
            if _cols is None:
                if data.ndim == 1 or data.shape[1] == 1:
                    _cols = ["value"]
                else:
                    _cols = ["_%s" % i for i in range(1, data.shape[1] + 1)]

            if data.ndim == 1:
                if 1 != len(_cols):
                    raise PySparkValueError(
                        error_class="AXIS_LENGTH_MISMATCH",
                        message_parameters={
                            "expected_length": str(len(_cols)),
                            "actual_length": "1",
                        },
                    )

                _table = pa.Table.from_arrays([pa.array(data)], _cols)
            else:
                if data.shape[1] != len(_cols):
                    raise PySparkValueError(
                        error_class="AXIS_LENGTH_MISMATCH",
                        message_parameters={
                            "expected_length": str(len(_cols)),
                            "actual_length": str(data.shape[1]),
                        },
                    )

                _table = pa.Table.from_arrays(
                    [pa.array(data[::, i]) for i in range(0, data.shape[1])], _cols
                )

            # The _table should already have the proper column names.
            _cols = None

        else:
            _data = list(data)

            if isinstance(_data[0], dict):
                # Sort the data to respect inferred schema.
                # For dictionaries, we sort the schema in alphabetical order.
                _data = [dict(sorted(d.items())) if d is not None else None for d in _data]

            elif not isinstance(_data[0], (Row, tuple, list, dict)) and not hasattr(
                _data[0], "__dict__"
            ):
                # input data can be [1, 2, 3]
                # we need to convert it to [[1], [2], [3]] to be able to infer schema.
                _data = [[d] for d in _data]

            if _schema is not None:
                if not isinstance(_schema, StructType):
                    _schema = StructType().add("value", _schema)
            else:
                _schema = self._inferSchemaFromList(_data, _cols)

                if _cols is not None and cast(int, _num_cols) < len(_cols):
                    _num_cols = len(_cols)

                if _has_nulltype(_schema):
                    # For cases like createDataFrame([("Alice", None, 80.1)], schema)
                    # we can not infer the schema from the data itself.
                    raise ValueError(
                        "Some of types cannot be determined after inferring, "
                        "a StructType Schema is required in this case"
                    )

            from pyspark.sql.connect.conversion import LocalDataToArrowConversion

            # Spark Connect will try its best to build the Arrow table with the
            # inferred schema in the client side, and then rename the columns and
            # cast the datatypes in the server side.
            _table = LocalDataToArrowConversion.convert(_data, _schema)

        # TODO: Beside the validation on number of columns, we should also check
        # whether the Arrow Schema is compatible with the user provided Schema.
        if _num_cols is not None and _num_cols != _table.shape[1]:
            raise PySparkValueError(
                error_class="AXIS_LENGTH_MISMATCH",
                message_parameters={
                    "expected_length": str(_num_cols),
                    "actual_length": str(_table.shape[1]),
                },
            )

        if _schema is not None:
            df = DataFrame.withPlan(LocalRelation(_table, schema=_schema.json()), self)
        else:
            df = DataFrame.withPlan(LocalRelation(_table), self)

        if _cols is not None and len(_cols) > 0:
            df = df.toDF(*_cols)
        return df

    createDataFrame.__doc__ = PySparkSession.createDataFrame.__doc__

    def sql(self, sqlQuery: str, args: Optional[Dict[str, Any]] = None) -> "DataFrame":
        cmd = SQL(sqlQuery, args)
        data, properties = self.client.execute_command(cmd.command(self._client))
        if "sql_command_result" in properties:
            return DataFrame.withPlan(CachedRelation(properties["sql_command_result"]), self)
        else:
            return DataFrame.withPlan(SQL(sqlQuery, args), self)

    sql.__doc__ = PySparkSession.sql.__doc__

    def range(
        self,
        start: int,
        end: Optional[int] = None,
        step: int = 1,
        numPartitions: Optional[int] = None,
    ) -> DataFrame:
        if end is None:
            actual_end = start
            start = 0
        else:
            actual_end = end

        if numPartitions is not None:
            numPartitions = int(numPartitions)

        return DataFrame.withPlan(
            Range(
                start=int(start), end=int(actual_end), step=int(step), num_partitions=numPartitions
            ),
            self,
        )

    range.__doc__ = PySparkSession.range.__doc__

    @property
    def catalog(self) -> "Catalog":
        from pyspark.sql.connect.catalog import Catalog

        if not hasattr(self, "_catalog"):
            self._catalog = Catalog(self)
        return self._catalog

    catalog.__doc__ = PySparkSession.catalog.__doc__

    def __del__(self) -> None:
        try:
            # Try its best to close.
            self.client.close()
        except Exception:
            pass

    def interrupt_all(self) -> None:
        self.client.interrupt_all()

    def stop(self) -> None:
        # Stopping the session will only close the connection to the current session (and
        # the life cycle of the session is maintained by the server),
        # whereas the regular PySpark session immediately terminates the Spark Context
        # itself, meaning that stopping all Spark sessions.
        # It is controversial to follow the existing the regular Spark session's behavior
        # specifically in Spark Connect the Spark Connect server is designed for
        # multi-tenancy - the remote client side cannot just stop the server and stop
        # other remote clients being used from other users.
        global _active_spark_session
        self.client.close()
        _active_spark_session = None

        if "SPARK_LOCAL_REMOTE" in os.environ:
            # When local mode is in use, follow the regular Spark session's
            # behavior by terminating the Spark Connect server,
            # meaning that you can stop local mode, and restart the Spark Connect
            # client with a different remote address.
            active_session = PySparkSession.getActiveSession()
            if active_session is not None:
                active_session.stop()
            with SparkContext._lock:
                del os.environ["SPARK_LOCAL_REMOTE"]
                del os.environ["SPARK_CONNECT_MODE_ENABLED"]
                if "SPARK_REMOTE" in os.environ:
                    del os.environ["SPARK_REMOTE"]

    stop.__doc__ = PySparkSession.stop.__doc__

    @classmethod
    def getActiveSession(cls) -> Any:
        raise PySparkNotImplementedError(
            error_class="NOT_IMPLEMENTED", message_parameters={"feature": "getActiveSession()"}
        )

    @property
    def conf(self) -> RuntimeConf:
        return RuntimeConf(self.client)

    @property
    def streams(self) -> "StreamingQueryManager":
        return StreamingQueryManager(self)

    def __getattr__(self, name: str) -> Any:
        if name in ["_jsc", "_jconf", "_jvm", "_jsparkSession"]:
            raise PySparkAttributeError(
                error_class="JVM_ATTRIBUTE_NOT_SUPPORTED", message_parameters={"attr_name": name}
            )
        elif name in ["newSession", "sparkContext"]:
            raise PySparkNotImplementedError(
                error_class="NOT_IMPLEMENTED", message_parameters={"feature": f"{name}()"}
            )
        return object.__getattribute__(self, name)

    @property
    def udf(self) -> "UDFRegistration":
        from pyspark.sql.connect.udf import UDFRegistration

        return UDFRegistration(self)

    udf.__doc__ = PySparkSession.udf.__doc__

    @property
    def version(self) -> str:
        result = self._client._analyze(method="spark_version").spark_version
        assert result is not None
        return result

    # SparkConnect-specific API
    @property
    def client(self) -> "SparkConnectClient":
        """
        Gives access to the Spark Connect client. In normal cases this is not necessary to be used
        and only relevant for testing.

        .. versionadded:: 3.4.0

        Returns
        -------
        :class:`SparkConnectClient`
        """
        return self._client

    def addArtifacts(
        self, *path: str, pyfile: bool = False, archive: bool = False, file: bool = False
    ) -> None:
        """
        Add artifact(s) to the client session. Currently only local files are supported.

        .. versionadded:: 3.5.0

        Parameters
        ----------
        *path : tuple of str
            Artifact's URIs to add.
        pyfile : bool
            Whether to add them as Python dependencies such as .py, .egg, .zip or .jar files.
            The pyfiles are directly inserted into the path when executing Python functions
            in executors.
        archive : bool
            Whether to add them as archives such as .zip, .jar, .tar.gz, .tgz, or .tar files.
            The archives are unpacked on the executor side automatically.
        file : bool
            Add a file to be downloaded with this Spark job on every node.
            The ``path`` passed can only be a local file for now.
        """
        if sum([file, pyfile, archive]) > 1:
            raise ValueError("'pyfile', 'archive' and/or 'file' cannot be True together.")
        self._client.add_artifacts(*path, pyfile=pyfile, archive=archive, file=file)

    addArtifact = addArtifacts

    def copyFromLocalToFs(self, local_path: str, dest_path: str) -> None:
        """
        Copy file from local to cloud storage file system.
        If the file already exits in destination path, old file is overwritten.

        Parameters
        ----------
        local_path: str
            Path to a local file. Directories are not supported.
            The path can be either an absolute path or a relative path.

        dest_path: str
            The cloud storage path to the destination the file will
            be copied to.
            The path must be an an absolute path.

        Notes
        -----
        This API is a developer API.
        """
        if urllib.parse.urlparse(dest_path).scheme:
            raise ValueError(
                "`spark_session.copyFromLocalToFs` API only allows `dest_path` to be a path "
                "without scheme, and spark driver uses the default scheme to "
                "determine the destination file system."
            )
        self._client.copy_from_local_to_fs(local_path, dest_path)

    @staticmethod
    def _start_connect_server(master: str, opts: Dict[str, Any]) -> None:
        """
        Starts the Spark Connect server given the master (thread-unsafe).

        At the high level, there are two cases. The first case is development case, e.g.,
        you locally build Apache Spark, and run ``SparkSession.builder.remote("local")``:

        1. This method automatically finds the jars for Spark Connect (because the jars for
          Spark Connect are not bundled in the regular Apache Spark release).

        2. Temporarily remove all states for Spark Connect, for example, ``SPARK_REMOTE``
          environment variable.

        3. Starts a JVM (without Spark Context) first, and adds the Spark Connect server jars
           into the current class loader. Otherwise, Spark Context with ``spark.plugins``
           cannot be initialized because the JVM is already running without the jars in
           the classpath before executing this Python process for driver side (in case of
           PySpark application submission).

        4. Starts a regular Spark session that automatically starts a Spark Connect server
           via ``spark.plugins`` feature.

        The second case is when you use Apache Spark release:

        1. Users must specify either the jars or package, e.g., ``--packages
          org.apache.spark:spark-connect_2.12:3.4.0``. The jars or packages would be specified
          in SparkSubmit automatically. This method does not do anything related to this.

        2. Temporarily remove all states for Spark Connect, for example, ``SPARK_REMOTE``
          environment variable. It does not do anything for PySpark application submission as
          well because jars or packages were already specified before executing this Python
          process for driver side.

        3. Starts a regular Spark session that automatically starts a Spark Connect server
          with JVM via ``spark.plugins`` feature.
        """
        session = PySparkSession._instantiatedSession
        if session is None or session._sc._jsc is None:

            # Configurations to be overwritten
            overwrite_conf = opts
            overwrite_conf["spark.master"] = master
            overwrite_conf["spark.local.connect"] = "1"

            # Configurations to be set if unset.
            default_conf = {"spark.plugins": "org.apache.spark.sql.connect.SparkConnectPlugin"}

            if "SPARK_TESTING" in os.environ:
                # For testing, we use 0 to use an ephemeral port to allow parallel testing.
                # See also SPARK-42272.
                overwrite_conf["spark.connect.grpc.binding.port"] = "0"

            def create_conf(**kwargs: Any) -> SparkConf:
                conf = SparkConf(**kwargs)
                for k, v in overwrite_conf.items():
                    conf.set(k, v)
                for k, v in default_conf.items():
                    if not conf.contains(k):
                        conf.set(k, v)
                return conf

            # Check if we're using unreleased version that is in development.
            # Also checks SPARK_TESTING for RC versions.
            is_dev_mode = (
                "dev" in LooseVersion(__version__).version or "SPARK_TESTING" in os.environ
            )

            origin_remote = os.environ.get("SPARK_REMOTE", None)
            try:
                if origin_remote is not None:
                    # So SparkSubmit thinks no remote is set in order to
                    # start the regular PySpark session.
                    del os.environ["SPARK_REMOTE"]

                SparkContext._ensure_initialized(conf=create_conf(loadDefaults=False))

                if is_dev_mode:
                    # Try and catch for a possibility in production because pyspark.testing
                    # does not exist in the canonical release.
                    try:
                        from pyspark.testing.utils import search_jar

                        # Note that, in production, spark.jars.packages configuration should be
                        # set by users. Here we're automatically searching the jars locally built.
                        connect_jar = search_jar(
                            "connector/connect/server", "spark-connect-assembly-", "spark-connect"
                        )
                        if connect_jar is None:
                            warnings.warn(
                                "Attempted to automatically find the Spark Connect jars because "
                                "'SPARK_TESTING' environment variable is set, or the current "
                                f"PySpark version is dev version ({__version__}). However, the jar"
                                " was not found. Manually locate the jars and specify them, e.g., "
                                "'spark.jars' configuration."
                            )
                        else:
                            pyutils = SparkContext._jvm.PythonSQLUtils  # type: ignore[union-attr]
                            pyutils.addJarToCurrentClassLoader(connect_jar)

                    except ImportError:
                        pass

                # The regular PySpark session is registered as an active session
                # so would not be garbage-collected.
                PySparkSession(
                    SparkContext.getOrCreate(create_conf(loadDefaults=True, _jvm=SparkContext._jvm))
                )
            finally:
                if origin_remote is not None:
                    os.environ["SPARK_REMOTE"] = origin_remote
        else:
            raise PySparkRuntimeError(
                error_class="SESSION_OR_CONTEXT_EXISTS",
                message_parameters={},
            )

    @property
    def session_id(self) -> str:
        return self._session_id


SparkSession.__doc__ = PySparkSession.__doc__


def _test() -> None:
    import sys
    import doctest
    from pyspark.sql import SparkSession as PySparkSession
    import pyspark.sql.connect.session

    globs = pyspark.sql.connect.session.__dict__.copy()
    globs["spark"] = (
        PySparkSession.builder.appName("sql.connect.session tests").remote("local[4]").getOrCreate()
    )

    # Uses PySpark session to test builder.
    globs["SparkSession"] = PySparkSession
    # Spark Connect does not support to set master together.
    pyspark.sql.connect.session.SparkSession.__doc__ = None
    del pyspark.sql.connect.session.SparkSession.Builder.master.__doc__
    # RDD API is not supported in Spark Connect.
    del pyspark.sql.connect.session.SparkSession.createDataFrame.__doc__

    # TODO(SPARK-41811): Implement SparkSession.sql's string formatter
    del pyspark.sql.connect.session.SparkSession.sql.__doc__

    (failure_count, test_count) = doctest.testmod(
        pyspark.sql.connect.session,
        globs=globs,
        optionflags=doctest.ELLIPSIS
        | doctest.NORMALIZE_WHITESPACE
        | doctest.IGNORE_EXCEPTION_DETAIL,
    )

    globs["spark"].stop()

    if failure_count:
        sys.exit(-1)


if __name__ == "__main__":
    _test()
