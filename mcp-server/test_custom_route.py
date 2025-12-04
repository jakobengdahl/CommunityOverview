from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
import asyncio

mcp = FastMCP("test")

@mcp.tool()
def echo(msg: str) -> str:
    return msg

# Try to add a custom route with correct signature
try:
    @mcp.custom_route("/chat", methods=["POST"])
    async def chat(request: Request):
        return JSONResponse({"response": "hello"})
    print("Added custom route")
except Exception as e:
    print(f"Failed to add custom route: {e}")
