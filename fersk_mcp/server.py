import os
from mcp.server import MCPServer
from im_bridge.lark_tools import sending_file
from config import CONFIG
from backend.logger import configure_mcp_logging

MCP_CONFIG = CONFIG["mcp"]
configure_mcp_logging(MCP_CONFIG["logLevel"])


mcp = MCPServer(
    name=MCP_CONFIG["name"],
    description=MCP_CONFIG["description"],
    version=MCP_CONFIG["version"],
)

mcp.add_tool(
    sending_file,
    name="sending_file",
    title="发送文件到飞书",
    description=(
        "根据绝对文件路径上传文件并发送给飞书用户。"
        "文件路径中必须包含目标用户或会话的 ou_ / oc_ 标识。"
    ),
)


if __name__ == "__main__":
    
    mcp.run(
        transport=MCP_CONFIG["transport"],
        host=os.getenv("MCP_HOST", MCP_CONFIG["host"]),
        port=int(os.getenv("MCP_PORT", str(MCP_CONFIG["port"]))),
        streamable_http_path=os.getenv("MCP_PATH", MCP_CONFIG["path"]),
    )
