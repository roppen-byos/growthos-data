"""Pinned Trino transport, .env loader, and closed monthly SQL queries."""

from __future__ import annotations

from collections import ChainMap
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from importlib.metadata import version
import hashlib
import logging
import math
import os
from pathlib import Path
import re
import sys
from threading import Event, Thread
import time
from typing import Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from growthos.contracts import BUSINESS_TIMEZONE, canonical_json, month, next_month
from growthos.errors import GrowthOSError, reject

PINNED_TRINO = "0.336.0"
BIKE_SERVICES = ("link",)
FUNNEL_BIKE_SERVICES = ("Link", "Bike Pink", "Bike Metro", "Scooty", "Bike Rated by Women", "Bike Lite")
FUNNEL_AUTO_SERVICES = ("Auto", "Auto Lite", "Auto Pet", "Auto Pool", "Auto Priority", "AutoPremium")
FUNNEL_CAB_SERVICES = (
    "CabEconomy", "CabPremium", "Cab Premium - Intercity", "Cab AC - Airport",
    "Cab AC Sedan - Airport", "Cab SUV", "Cab Premium - Airport", "Cab AC Sedan",
    "Cab Daily", "Cab Economy - Intercity", "CabAC", "Cab Priority", "Cab SUV - Airport",
    "Cab SUV - Intercity", "Cab Economy - Airport", "Cab Premium Biz",
)
OPERATOR_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
_SAFE_SQL_PARAM = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_SAFE_ERROR_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_CANONICAL_SETTINGS = (
    "GROWTHOS_TRINO_HOST", "GROWTHOS_TRINO_USER", "GROWTHOS_TRINO_CATALOG", "GROWTHOS_TRINO_PORT",
)
_FILE_KEYS = {name: name for name in _CANONICAL_SETTINGS}
_FILE_KEYS.update({name.removeprefix("GROWTHOS_"): name for name in _CANONICAL_SETTINGS})


def _canonical_city_sql(column: str) -> str:
    slug = f"coalesce(nullif(trim(BOTH '_' FROM regexp_replace(lower(trim({column})), '[^a-z0-9]+', '_')), ''), 'city_not_found')"
    return (
        f"CASE {slug} "
        "WHEN 'guntur' THEN 'vijayawada' "
        "WHEN 'faridabad' THEN 'delhi' "
        "WHEN 'attingal' THEN 'trivandrum' "
        f"ELSE {slug} END"
    )


def _service_list(names: tuple[str, ...]) -> str:
    return ", ".join("'" + name.replace("'", "''") + "'" for name in names)


@dataclass(frozen=True)
class MonthWindow:
    value: str

    def __post_init__(self):
        start = month(self.value)
        current = datetime.now(timezone.utc).astimezone(ZoneInfo(BUSINESS_TIMEZONE)).date().replace(day=1)
        if start >= current:
            reject("INCOMPLETE_MONTH", "Warehouse extraction requires explicitly ended calendar months.")

    @property
    def start(self):
        return month(self.value)

    @property
    def end(self):
        return next_month(self.start)

    @property
    def day_parameters(self) -> tuple[str, str]:
        return tuple(f"{day.year:04d}{day.month:02d}{day.day:02d}" for day in (self.start, self.end))

    @property
    def date_parameters(self) -> tuple[str, str]:
        return self.start.isoformat(), self.end.isoformat()

    def evidence(self) -> dict:
        return {
            "month": self.value,
            "start_date": self.start.isoformat(),
            "end_date_exclusive": self.end.isoformat(),
            "business_timezone": BUSINESS_TIMEZONE,
        }


class Operation(str, Enum):
    AUTHORIZE = "authorization-probe"
    INSTALLS_MONTHLY = "installs-monthly"
    REGISTRATIONS_MONTHLY = "registrations-monthly"
    FTU_MONTHLY = "ftu-monthly"
    FUNNEL_MONTHLY = "funnel-monthly"


EXTRACTION_OPERATIONS = (
    Operation.INSTALLS_MONTHLY,
    Operation.REGISTRATIONS_MONTHLY,
    Operation.FTU_MONTHLY,
)
FUNNEL_OPERATIONS = (Operation.FUNNEL_MONTHLY,)
ALL_OPERATIONS = EXTRACTION_OPERATIONS + FUNNEL_OPERATIONS

_INSTALLS_MONTHLY = f"""
WITH mapped AS (
    SELECT {_canonical_city_sql("m.city")} AS canonical_city_key,
           coalesce(nullif(trim(m.city), ''), 'city_not_found') AS source_city,
           i.appsflyer_id
    FROM appsflyer.canonical_appsflyer_customer_install i
    LEFT JOIN marketing.media_source_channel_mapping c
      ON c.identifier = coalesce(i.media_source, i.af_prt, 'organic')
    LEFT JOIN marketing.appsflyer_vs_rapido_internal_mapping m
      ON af_pincode = postal_code
    WHERE i.yyyymmdd >= ? AND i.yyyymmdd < ?
      AND c.channel NOT IN ('Retargeting_repeat', 'Retargeting_new')
      AND length(i.appsflyer_id) != 14
)
SELECT canonical_city_key,
       min(source_city) AS sample_source_city,
       count(DISTINCT appsflyer_id) AS installs
FROM mapped
GROUP BY canonical_city_key
ORDER BY canonical_city_key
""".strip()

_REGISTRATIONS_MONTHLY = f"""
WITH registrations_ranked AS (
    SELECT {_canonical_city_sql("city")} AS canonical_city_key,
           coalesce(nullif(trim(city), ''), 'city_not_found') AS source_city,
           substr(mobile, -10, 10) AS last10,
           row_number() OVER (
               PARTITION BY substr(mobile, -10, 10) ORDER BY yyyymmdd ASC
           ) AS row3
    FROM entity.users_snapshot
    WHERE yyyymmdd >= ? AND yyyymmdd < ?
      AND mobile_verified = TRUE AND roles LIKE '%customer%'
)
SELECT canonical_city_key,
       min(source_city) AS sample_source_city,
       count(DISTINCT last10) AS registrations
FROM registrations_ranked
WHERE row3 = 1
GROUP BY canonical_city_key
ORDER BY canonical_city_key
""".strip()

_FTU_MONTHLY = f"""
WITH mapped AS (
    SELECT {_canonical_city_sql("city_name")} AS canonical_city_key,
           coalesce(nullif(trim(city_name), ''), 'city_not_found') AS source_city,
           customer_id,
           lower(trim(service_category)) AS service_key,
           platform_level_new_order_flag
    FROM customer.customer_order_channel_attribution_master
    WHERE cast(order_date AS varchar) >= ? AND cast(order_date AS varchar) < ?
)
SELECT canonical_city_key,
       min(source_city) AS sample_source_city,
       count(DISTINCT CASE WHEN platform_level_new_order_flag = 'new' THEN customer_id END) AS platform_ftu,
       count(DISTINCT CASE WHEN platform_level_new_order_flag = 'new' AND service_key = 'link' THEN customer_id END) AS platform_link_ftu,
       count(DISTINCT CASE WHEN platform_level_new_order_flag = 'new' AND service_key = 'auto' THEN customer_id END) AS platform_auto_ftu,
       count(DISTINCT CASE WHEN platform_level_new_order_flag = 'new' AND service_key = 'cab' THEN customer_id END) AS platform_cab_ftu
FROM mapped
GROUP BY canonical_city_key
ORDER BY canonical_city_key
""".strip()

_FUNNEL_MONTHLY = f"""
WITH rf AS (
    SELECT date_trunc('day', date(day)) AS time_value,
           customerid,
           city,
           CAST(max(fe_sessions_unique_daily) AS double) AS fe,
           CAST(sum(rr_sessions_unique_daily) AS double) AS rr,
           CAST(sum(net_rides_daily) AS double) AS net,
           CAST(sum(CASE WHEN service_name IN ({_service_list(FUNNEL_BIKE_SERVICES)}) THEN rr_sessions_unique_daily END) AS double) AS bike_rr,
           CAST(sum(CASE WHEN service_name IN ({_service_list(FUNNEL_BIKE_SERVICES)}) THEN net_rides_daily END) AS double) AS bike_net,
           CAST(sum(CASE WHEN service_name IN ({_service_list(FUNNEL_AUTO_SERVICES)}) THEN rr_sessions_unique_daily END) AS double) AS auto_rr,
           CAST(sum(CASE WHEN service_name IN ({_service_list(FUNNEL_AUTO_SERVICES)}) THEN net_rides_daily END) AS double) AS auto_net,
           CAST(sum(CASE WHEN service_name IN ({_service_list(FUNNEL_CAB_SERVICES)}) THEN rr_sessions_unique_daily END) AS double) AS cab_rr,
           CAST(sum(CASE WHEN service_name IN ({_service_list(FUNNEL_CAB_SERVICES)}) THEN net_rides_daily END) AS double) AS cab_net
    FROM hive.datasets.customer_rf_daily_kpi_old_ct
    WHERE day >= ? AND day < ?
    GROUP BY 1,2,3
), city_month AS (
    SELECT {_canonical_city_sql("city")} AS canonical_city_key,
           coalesce(nullif(trim(city), ''), 'city_not_found') AS source_city,
           CAST(coalesce(sum(fe), 0) AS bigint) AS fe_sessions,
           CAST(coalesce(sum(rr), 0) AS bigint) AS rr,
           CAST(coalesce(sum(net), 0) AS bigint) AS net,
           CAST(coalesce(sum(bike_rr), 0) AS bigint) AS bike_rr,
           CAST(coalesce(sum(bike_net), 0) AS bigint) AS bike_net,
           CAST(coalesce(sum(auto_rr), 0) AS bigint) AS auto_rr,
           CAST(coalesce(sum(auto_net), 0) AS bigint) AS auto_net,
           CAST(coalesce(sum(cab_rr), 0) AS bigint) AS cab_rr,
           CAST(coalesce(sum(cab_net), 0) AS bigint) AS cab_net
    FROM rf
    GROUP BY 1,2
    ltr_data as (
            SELECT date_trunc(lower('day'),date(platform_level_first_order_date)) as time_value, customer_id
            from customer_internal.customer_first_ride_lookup_immutable
            where day = date({{end_date}})
            group by 1,2
    )
)
SELECT canonical_city_key,
       min(source_city) AS sample_source_city,
       sum(fe_sessions) AS fe_sessions,
       sum(rr) AS rr,
       sum(net) AS net,
       sum(bike_rr) AS bike_rr,
       sum(bike_net) AS bike_net,
       sum(auto_rr) AS auto_rr,
       sum(auto_net) AS auto_net,
       sum(cab_rr) AS cab_rr,
       sum(cab_net) AS cab_net
FROM city_month
GROUP BY canonical_city_key
ORDER BY canonical_city_key
""".strip()


@dataclass(frozen=True)
class QuerySpec:
    sql: str
    parameters: tuple
    columns: tuple[str, ...]
    kind: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()

    def render(self) -> str:
        parts = self.sql.split("?")
        if len(parts) - 1 != len(self.parameters):
            reject("OPERATION", "Warehouse query parameter count mismatch.")
        if not self.parameters:
            return self.sql
        rendered = [parts[0]]
        for param, tail in zip(self.parameters, parts[1:]):
            if not isinstance(param, str) or not _SAFE_SQL_PARAM.fullmatch(param):
                reject("OPERATION", "Warehouse query parameter is not an allowlisted literal.")
            rendered.append(f"'{param}'")
            rendered.append(tail)
        return "".join(rendered)


def query_for(operation: Operation, window: MonthWindow) -> QuerySpec:
    if not isinstance(operation, Operation) or not isinstance(window, MonthWindow):
        reject("OPERATION", "Only registered warehouse operations and validated month windows are supported.")
    if operation == Operation.AUTHORIZE:
        return QuerySpec("SELECT 1 AS ready", (), ("ready",), "authorization")
    if operation == Operation.INSTALLS_MONTHLY:
        return QuerySpec(_INSTALLS_MONTHLY, window.day_parameters, (
            "canonical_city_key", "sample_source_city", "installs",
        ), "city_counts")
    if operation == Operation.REGISTRATIONS_MONTHLY:
        return QuerySpec(_REGISTRATIONS_MONTHLY, window.day_parameters, (
            "canonical_city_key", "sample_source_city", "registrations",
        ), "city_counts")
    if operation == Operation.FTU_MONTHLY:
        return QuerySpec(_FTU_MONTHLY, window.date_parameters, (
            "canonical_city_key", "sample_source_city", "platform_ftu",
            "platform_link_ftu", "platform_auto_ftu", "platform_cab_ftu",
        ), "city_counts")
    if operation == Operation.FUNNEL_MONTHLY:
        return QuerySpec(_FUNNEL_MONTHLY, window.date_parameters, (
            "canonical_city_key", "sample_source_city", "fe_sessions", "rr", "net",
            "bike_rr", "bike_net", "auto_rr", "auto_net", "cab_rr", "cab_net",
        ), "city_counts")
    reject("OPERATION", "Unsupported warehouse operation.")


def read_operator_file(path: Path) -> dict[str, str]:
    """Read the four non-token Trino settings from gwos-data/.env if present."""
    try:
        if path.is_symlink():
            reject("WAREHOUSE_CONFIG_FILE", "Operator .env must be a regular, nonsymlink file.")
        raw = path.read_bytes()
        if len(raw) > 65_536:
            reject("WAREHOUSE_CONFIG_FILE", "Operator .env exceeds size limit.")
        content = raw.decode("utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        reject("WAREHOUSE_CONFIG_FILE", "Operator .env could not be read safely.")

    selected: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
        if match is None:
            reject("WAREHOUSE_CONFIG_FILE", "Invalid operator .env syntax.")
        key, val = match.groups()
        if key not in _FILE_KEYS:
            continue
        canonical = _FILE_KEYS[key]
        if canonical in selected:
            reject("WAREHOUSE_CONFIG_FILE", "Duplicate operator setting in .env.")
        val = val.strip()
        if len(val) >= 2 and val[0] in ("'", '"') and val[-1] == val[0]:
            val = val[1:-1]
        else:
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        selected[canonical] = val
    return selected


def _safe_exception_detail(exc: BaseException) -> str:
    details = [type(exc).__name__]
    for attribute in ("error_type", "error_name"):
        value = getattr(exc, attribute, None)
        if isinstance(value, str) and _SAFE_ERROR_TOKEN.fullmatch(value):
            details.append(f"{attribute}={value}")
    error_code = getattr(exc, "error_code", None)
    if isinstance(error_code, int) and not isinstance(error_code, bool):
        details.append(f"error_code={error_code}")
    return "; ".join(details)


@dataclass(frozen=True)
class WarehouseConfig:
    host: str
    user: str
    catalog: str = "hive"
    port: int = 443
    request_timeout: float = 30
    query_timeout: float = 120
    authorization_timeout: float = 180
    total_timeout: float = 600

    def __post_init__(self):
        if not isinstance(self.host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", self.host):
            reject("WAREHOUSE_CONFIG", "Set GROWTHOS_TRINO_HOST to a hostname only (no https:// or port); set port with GROWTHOS_TRINO_PORT.")
        if not isinstance(self.user, str) or not 1 <= len(self.user) <= 320 or any(not c.isprintable() or c.isspace() for c in self.user):
            reject("WAREHOUSE_CONFIG", "An explicit operator warehouse user is required.")
        if not isinstance(self.catalog, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", self.catalog):
            reject("WAREHOUSE_CONFIG", "Warehouse catalog must be a bounded identifier.")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            reject("WAREHOUSE_CONFIG", "Warehouse port is invalid.")
        for value, maximum in ((self.request_timeout, 30), (self.query_timeout, 120), (self.authorization_timeout, 180), (self.total_timeout, 600)):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0.01 <= value <= maximum:
                reject("WAREHOUSE_CONFIG", "Warehouse time budgets must be finite, positive and within approved caps.")


def operator_environment(environ=None, *, env_file=None) -> WarehouseConfig:
    env = os.environ if environ is None else environ
    allowed = set(_CANONICAL_SETTINGS)
    forbidden = ("GROWTHOS_READER_", "GROWTHOS_WRITER_", "GROWTHOS_TEST_", "TRINO_", "NEON_", "VERCEL_", "PG", "DATABASE_URL")
    if any(value and (any(key.startswith(p) for p in forbidden) or (key.startswith("GROWTHOS_TRINO_") and key not in allowed)) for key, value in env.items()):
        reject("CREDENTIAL_BOUNDARY", "Run extraction in a clean operator environment without reporting or provider credentials.")
    if any(env.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")):
        reject("NETWORK_POLICY_REVIEW", "An ambient proxy is configured but not supported by this connector.")
    if env_file is not None:
        env = ChainMap(read_operator_file(env_file), env)
    host = env.get("GROWTHOS_TRINO_HOST") or ""
    user = env.get("GROWTHOS_TRINO_USER") or ""
    if not host or not user:
        missing = [k for k, v in (("GROWTHOS_TRINO_HOST", host), ("GROWTHOS_TRINO_USER", user)) if not v]
        reject("WAREHOUSE_CONFIG_MISSING", f"Set {', '.join(missing)} in gwos-data/.env or your operator environment.")
    try:
        port = int(env.get("GROWTHOS_TRINO_PORT", "443"))
    except (TypeError, ValueError):
        reject("WAREHOUSE_CONFIG", "Warehouse port must be an integer.")
    return WarehouseConfig(host=host, user=user, catalog=env.get("GROWTHOS_TRINO_CATALOG", "hive"), port=port)


def show_authorization_url(url: str) -> None:
    """Print OAuth URL to interactive stderr for manual Windows-browser approval in WSL."""
    try:
        parts = urlsplit(url) if isinstance(url, str) and len(url) <= 16384 else None
        valid = bool(parts and parts.scheme == "https" and parts.hostname and not parts.username and not parts.password and all(c.isprintable() and not c.isspace() for c in url))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        reject("OAUTH_REDIRECT", "OAuth requires a verified HTTPS authorization URL; URL withheld.")
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        reject("OPERATOR_TERMINAL", "Authorization links are displayed only in the human operator's interactive terminal.")
    try:
        print(
            "Open this approval link in your Windows browser, then leave this CLI running:\n"
            + url + "\nKeep the link private; do not paste it into chat or logs.",
            file=sys.stderr, flush=True,
        )
    except Exception:
        reject("OAUTH_TERMINAL", "Could not display the approval link in the operator terminal.")


def _default_connection(config: WarehouseConfig, handler: Callable[[str], None]):
    try:
        if version("trino") != PINNED_TRINO:
            reject("DEPENDENCY", "Warehouse requires the reviewed pinned Trino client version.")
        import trino.auth
        import trino.dbapi
    except ImportError:
        reject("DEPENDENCY", "Install the optional warehouse dependency in the operator environment.")
    logging.disable(logging.CRITICAL)
    authentication = trino.auth.OAuth2Authentication(redirect_auth_url_handler=handler)
    bearer = getattr(authentication, "_bearer", None)
    if type(getattr(bearer, "_token_cache", None)) is not trino.auth._OAuth2TokenInMemoryCache:
        reject("OAUTH_CACHE", "Warehouse OAuth requires process-memory-only token caching.")
    bearer.MAX_OAUTH_ATTEMPTS = max(getattr(bearer, "MAX_OAUTH_ATTEMPTS", 5), 25)
    try:
        connection = trino.dbapi.connect(
            host=config.host, port=config.port, user=config.user, catalog=config.catalog,
            http_scheme="https", verify=True, auth=authentication,
            request_timeout=config.request_timeout, max_attempts=1,
        )
        http_session = getattr(connection, "_http_session", None)
        if hasattr(http_session, "trust_env"):
            http_session.trust_env = False
        return connection
    except Exception:
        reject("WAREHOUSE_CONNECT", "Warehouse connection construction failed; raw diagnostics withheld.")


def _bounded_string(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 120 and all(c.isprintable() for c in value)


def _validate_rows(spec: QuerySpec, rows: object) -> list[dict]:
    if not isinstance(rows, (list, tuple)):
        reject("WAREHOUSE_SCHEMA", "Warehouse aggregate rows are unavailable; no partial evidence emitted.")
    if spec.kind == "authorization" and len(rows) != 1:
        reject("WAREHOUSE_SCHEMA", "A scalar aggregate must return exactly one row.")
    result, grain = [], set()
    for raw in rows:
        if not isinstance(raw, (list, tuple)) or len(raw) != len(spec.columns):
            reject("WAREHOUSE_SCHEMA", "Warehouse aggregate shape is invalid; raw fields and values withheld.")
        row = dict(zip(spec.columns, raw))
        count_fields = spec.columns
        if spec.kind == "city_counts":
            if not _bounded_string(raw[0]) or not _bounded_string(raw[1]):
                reject("WAREHOUSE_VALUE", "Invalid canonical or source city label; raw value withheld.")
            count_fields = spec.columns[2:]
            if raw[0] in grain:
                reject("WAREHOUSE_GRAIN", "Duplicate warehouse aggregate grain; results were not combined.")
            grain.add(raw[0])
        for field in count_fields:
            value = row[field]
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                reject("WAREHOUSE_VALUE", "Warehouse counts must be nonnegative 64-bit integers.")
        if spec.kind == "authorization" and row["ready"] != 1:
            reject("WAREHOUSE_AUTH", "Warehouse authorization probe did not succeed.")
        result.append(row)
    return result


class WarehouseSession:
    """One-use session. Injected factories are a nonproduction testing seam only."""

    def __init__(self, config: WarehouseConfig, *, connection_factory=None, live_authorized=False,
                 interactive=False, authorization_handler=show_authorization_url, cancel_event=None):
        self.config = config
        self._factory = connection_factory
        self._live_authorized = live_authorized
        self._interactive = interactive
        self._handler = authorization_handler
        self._cancel = cancel_event if cancel_event is not None else Event()
        self._connection = None
        self._cursor = None
        self._authorized = False
        self._windows: set[str] = set()
        self._closed = False
        self._started = time.monotonic()

    @property
    def nonproduction(self) -> bool:
        return self._factory is not None

    @property
    def endpoint_fingerprint(self) -> str:
        return hashlib.sha256(f"{self.config.host}:{self.config.port}/{self.config.catalog}".encode()).hexdigest()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _checkpoint(self):
        if self._cancel.is_set():
            reject("CANCELLED", "Warehouse extraction cancelled; no publication is available.")
        if self._closed:
            reject("WAREHOUSE_SESSION", "Warehouse session is closed and cannot be reused.")
        if time.monotonic() - self._started >= self.config.total_timeout:
            reject("WAREHOUSE_TIMEOUT", "Warehouse extraction exceeded its total time budget.")

    def _blocking(self, work, deadline: float, operation_name: str = "warehouse"):
        self._checkpoint()
        done, outcome = Event(), []

        def invoke():
            try:
                outcome.append((True, work()))
            except GrowthOSError as error:
                outcome.append((False, error))
            except BaseException as exc:
                detail = _safe_exception_detail(exc)
                code = "WAREHOUSE_AUTH" if type(exc).__name__ == "TrinoAuthError" else "WAREHOUSE_QUERY"
                outcome.append((False, GrowthOSError(code, f"Warehouse {operation_name} failed ({detail}); raw messages withheld.")))
            finally:
                done.set()

        Thread(target=invoke, name="growthos-warehouse-request", daemon=True).start()
        try:
            while not done.wait(0.02):
                self._checkpoint()
                if time.monotonic() >= deadline:
                    reject("WAREHOUSE_TIMEOUT", "Warehouse operation exceeded its time budget.")
            self._checkpoint()
            if time.monotonic() >= deadline:
                reject("WAREHOUSE_TIMEOUT", "Warehouse operation exceeded its time budget.")
        except BaseException:
            self.close()
            raise
        success, value = outcome[0]
        if not success:
            self.close()
            raise value from None
        return value

    def authorize(self, window: MonthWindow, extra_windows: tuple[MonthWindow, ...] = ()):
        self._checkpoint()
        if not isinstance(window, MonthWindow) or any(not isinstance(w, MonthWindow) for w in extra_windows):
            reject("OPERATION", "Warehouse authorization requires a validated monthly window.")
        if self._connection is not None or self._authorized:
            reject("WAREHOUSE_SESSION", "Authorize a warehouse session only once.")
        if self._factory is None and not (self._live_authorized and self._interactive):
            reject("LIVE_AUTHORIZATION", "Real warehouse access requires explicit live authorization from the operator's terminal.")
        try:
            factory = self._factory or _default_connection
            self._connection = factory(self.config, lambda url: (self._checkpoint(), self._handler(url), self._checkpoint()))
        except GrowthOSError:
            self.close()
            raise
        except Exception:
            self.close()
            reject("WAREHOUSE_CONNECT", "Warehouse connection construction failed; raw diagnostics withheld.")
        self._execute(Operation.AUTHORIZE, window, self.config.authorization_timeout)
        self._authorized = True
        self._windows = {window.value, *(w.value for w in extra_windows)}

    def run(self, operation: Operation, window: MonthWindow) -> dict:
        self._checkpoint()
        if not self._authorized or operation == Operation.AUTHORIZE:
            reject("WAREHOUSE_AUTH", "Authorize once before requesting registered aggregate operations.")
        if not isinstance(window, MonthWindow) or window.value not in self._windows:
            reject("REQUEST_MISMATCH", "The source session is pinned to the explicitly authorized month window.")
        return self._execute(operation, window, self.config.query_timeout)

    def _execute(self, operation: Operation, window: MonthWindow, budget: float) -> dict:
        spec = query_for(operation, window)
        rendered_sql = spec.render()
        started_at = datetime.now(timezone.utc).isoformat()
        deadline = time.monotonic() + budget
        try:
            self._cursor = self._connection.cursor()
        except Exception:
            self.close()
            reject("WAREHOUSE_QUERY", "Warehouse cursor could not be opened; diagnostics withheld.")
        cursor = self._cursor

        def fetch():
            self._checkpoint()
            cursor.execute(rendered_sql)
            self._checkpoint()
            description = cursor.description
            if not isinstance(description, (list, tuple)) or any(not isinstance(c, (list, tuple)) or not c for c in description):
                reject("WAREHOUSE_SCHEMA", "Warehouse aggregate schema is unavailable; no rows fetched.")
            if tuple(c[0] for c in description) != spec.columns:
                reject("WAREHOUSE_SCHEMA", "Unexpected aggregate columns; no rows fetched and raw fields withheld.")
            rows = cursor.fetchall()
            self._checkpoint()
            checked = _validate_rows(spec, rows)
            cursor.close()
            return checked

        try:
            rows = self._blocking(fetch, deadline, operation.value)
        except BaseException:
            self._cursor = None
            self._cleanup(cursor=cursor, cancel=True)
            self.close()
            raise
        self._cursor = None
        return {
            "operation": operation.value, "query_checksum": spec.checksum,
            "result_checksum": hashlib.sha256(canonical_json(rows)).hexdigest(),
            "catalog": self.config.catalog, "endpoint_fingerprint": self.endpoint_fingerprint,
            "window": window.evidence(), "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(), "nonproduction": self.nonproduction,
            "rows": rows, "row_count": len(rows), "omitted_row_count": 0,
        }

    @staticmethod
    def _cleanup(*, cursor=None, connection=None, cancel=False):
        def dispose(resource, should_cancel):
            if should_cancel:
                try:
                    resource.cancel()
                except BaseException:
                    pass
            try:
                resource.close()
            except BaseException:
                pass
        for resource in (cursor, connection):
            if resource is not None:
                Thread(target=dispose, args=(resource, cancel and resource is cursor), name="growthos-warehouse-close", daemon=True).start()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._authorized = False
        self._cancel.set()
        cursor, connection = self._cursor, self._connection
        self._cursor = self._connection = None
        self._cleanup(cursor=cursor, connection=connection, cancel=True)
