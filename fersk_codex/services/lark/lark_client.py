import os

import lark_oapi as lark
from fersk_codex.utils.config_loader import CONFIG


def _required_setting(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少飞书配置项: {name}")
    return value


LARK_APP_ID = _required_setting(CONFIG["lark"]["credentials"]["appIdEnv"])
LARK_APP_SECRET = _required_setting(CONFIG["lark"]["credentials"]["appSecretEnv"])


# OpenAPI client: used for normal HTTP calls such as sending messages and
# downloading message resources.
client = (
    lark.Client.builder()
    .app_id(LARK_APP_ID)
    .app_secret(LARK_APP_SECRET)
    .timeout(CONFIG["codex"]["watchdog"]["cardRequestTimeoutSeconds"])
    .log_level(lark.LogLevel.DEBUG)
    .build()
)


def create_websocket_client(
    event_handler: lark.EventDispatcherHandler,
) -> lark.ws.Client:
    """Build the event transport with the same app credentials."""
    return lark.ws.Client(
        LARK_APP_ID,
        LARK_APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG,
    )
