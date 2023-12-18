from typing import Any, ClassVar, Dict, Optional, Type

import attrs

from data_diff.databases.base import (
    MD5_HEXDIGITS,
    CHECKSUM_HEXDIGITS,
    TIMESTAMP_PRECISION_POS,
    CHECKSUM_OFFSET,
    BaseDialect,
    ThreadedDatabase,
    import_helper,
    ConnectError,
)
from data_diff.abcs.database_types import (
    ColType,
    DbPath,
    Decimal,
    Float,
    Integer,
    FractionalType,
    Native_UUID,
    TemporalType,
    Text,
    Timestamp,
    Boolean,
)
from data_diff.schema import RawColumnInfo

# https://clickhouse.com/docs/en/operations/server-configuration-parameters/settings/#default-database
DEFAULT_DATABASE = "default"


@import_helper("clickhouse")
def import_clickhouse():
    import clickhouse_driver

    return clickhouse_driver


@attrs.define(frozen=False)
class Dialect(BaseDialect):
    name = "Clickhouse"
    ROUNDS_ON_PREC_LOSS = False
    TYPE_CLASSES = {
        "Int8": Integer,
        "Int16": Integer,
        "Int32": Integer,
        "Int64": Integer,
        "Int128": Integer,
        "Int256": Integer,
        "UInt8": Integer,
        "UInt16": Integer,
        "UInt32": Integer,
        "UInt64": Integer,
        "UInt128": Integer,
        "UInt256": Integer,
        "Float32": Float,
        "Float64": Float,
        "Decimal": Decimal,
        "UUID": Native_UUID,
        "String": Text,
        "FixedString": Text,
        "DateTime": Timestamp,
        "DateTime64": Timestamp,
        "Bool": Boolean,
    }

    def quote(self, s: str) -> str:
        return f'"{s}"'

    def to_string(self, s: str) -> str:
        return f"toString({s})"

    def _convert_db_precision_to_digits(self, p: int) -> int:
        # Done the same as for PostgreSQL but need to rewrite in another way
        # because it does not help for float with a big integer part.
        return super()._convert_db_precision_to_digits(p) - 2

    def parse_type(self, table_path: DbPath, info: RawColumnInfo) -> ColType:
        nullable_prefix = "Nullable("
        if info.type_repr.startswith(nullable_prefix):
            info = attrs.evolve(info, data_type=info.type_repr[len(nullable_prefix) :].rstrip(")"))

        if info.type_repr.startswith("Decimal"):
            info = attrs.evolve(info, data_type="Decimal")
        elif info.type_repr.startswith("FixedString"):
            info = attrs.evolve(info, data_type="FixedString")
        elif info.type_repr.startswith("DateTime64"):
            info = attrs.evolve(info, data_type="DateTime64")

        return super().parse_type(table_path, info)

    # def timestamp_value(self, t: DbTime) -> str:
    #     # return f"'{t}'"
    #     return f"'{str(t)[:19]}'"

    def set_timezone_to_utc(self) -> str:
        raise NotImplementedError()

    def current_timestamp(self) -> str:
        return "now()"

    def md5_as_int(self, s: str) -> str:
        substr_idx = 1 + MD5_HEXDIGITS - CHECKSUM_HEXDIGITS
        return (
            f"reinterpretAsUInt128(reverse(unhex(lowerUTF8(substr(hex(MD5({s})), {substr_idx}))))) - {CHECKSUM_OFFSET}"
        )

    def md5_as_hex(self, s: str) -> str:
        return f"hex(MD5({s}))"

    def normalize_number(self, value: str, coltype: FractionalType) -> str:
        # If a decimal value has trailing zeros in a fractional part, when casting to string they are dropped.
        # For example:
        #   select toString(toDecimal128(1.10, 2));  -- the result is 1.1
        #   select toString(toDecimal128(1.00, 2)); -- the result is 1
        # So, we should use some custom approach to save these trailing zeros.
        # To avoid it, we can add a small value like 0.000001 to prevent dropping of zeros from the end when casting.
        # For examples above it looks like:
        #   select toString(toDecimal128(1.10, 2 + 1) + toDecimal128(0.001, 3)); -- the result is 1.101
        # After that, cut an extra symbol from the string, i.e. 1.101 -> 1.10
        # So, the algorithm is:
        # 1. Cast to decimal with precision + 1
        # 2. Add a small value 10^(-precision-1)
        # 3. Cast the result to string
        # 4. Drop the extra digit from the string. To do that, we need to slice the string
        #    with length = digits in an integer part + 1 (symbol of ".") + precision

        if coltype.precision == 0:
            return self.to_string(f"round({value})")

        precision = coltype.precision
        # TODO: too complex, is there better performance way?
        value = f"""
            if({value} >= 0, '', '-') || left(
                toString(
                    toDecimal128(
                        round(abs({value}), {precision}),
                        {precision} + 1
                    )
                    +
                    toDecimal128(
                        exp10(-{precision + 1}),
                        {precision} + 1
                    )
                ),
                toUInt8(
                    greatest(
                        floor(log10(abs({value}))) + 1,
                        1
                    )
                ) + 1 + {precision}
            )
        """
        return value

    def normalize_timestamp(self, value: str, coltype: TemporalType) -> str:
        prec = coltype.precision
        if coltype.rounds:
            timestamp = f"toDateTime64(round(toUnixTimestamp64Micro(toDateTime64({value}, 6)) / 1000000, {prec}), 6)"
            return self.to_string(timestamp)

        fractional = f"toUnixTimestamp64Micro(toDateTime64({value}, {prec})) % 1000000"
        fractional = f"lpad({self.to_string(fractional)}, 6, '0')"
        value = f"formatDateTime({value}, '%Y-%m-%d %H:%M:%S') || '.' || {self.to_string(fractional)}"
        return f"rpad({value}, {TIMESTAMP_PRECISION_POS + 6}, '0')"


@attrs.define(frozen=False, init=False, kw_only=True)
class Clickhouse(ThreadedDatabase):
    DIALECT_CLASS: ClassVar[Type[BaseDialect]] = Dialect
    CONNECT_URI_HELP = "clickhouse://<user>:<password>@<host>/<database>"
    CONNECT_URI_PARAMS = ["database?"]

    _args: Dict[str, Any]

    def __init__(self, *, thread_count: int, **kw):
        super().__init__(thread_count=thread_count)

        self._args = kw
        # In Clickhouse database and schema are the same
        self.default_schema = kw.get("database", DEFAULT_DATABASE)

    def create_connection(self):
        clickhouse = import_clickhouse()

        class SingleConnection(clickhouse.dbapi.connection.Connection):
            """Not thread-safe connection to Clickhouse"""

            def cursor(self, cursor_factory=None):
                if not len(self.cursors):
                    _ = super().cursor()
                return self.cursors[0]

        try:
            return SingleConnection(**self._args)
        except clickhouse.OperationError as e:
            raise ConnectError(*e.args) from e

    @property
    def is_autocommit(self) -> bool:
        return True
