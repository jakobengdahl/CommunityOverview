#!/usr/bin/env python3
"""
Run the Community Knowledge Graph server.

This script starts the unified server that exposes GraphService
over both REST API and MCP protocol.

Usage:
    python run_server.py
    python run_server.py --port 8080
    python run_server.py --graph-file custom_graph.json

Environment variables:
    GRAPH_FILE: Path to graph JSON file (default: graph.json)
    HOST: Server host (default: 0.0.0.0)
    PORT: Server port (default: 8000)
    API_PREFIX: REST API prefix (default: /api)
"""

import argparse
import os
import sys

import uvicorn

from app_host import create_app, AppConfig


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run the Community Knowledge Graph server"
    )
    parser.add_argument(
        "--host",
        default=os.getenv("HOST", "0.0.0.0"),
        help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "8000")),
        help="Port to bind to (default: 8000)"
    )
    parser.add_argument(
        "--graph-file",
        default=os.getenv("GRAPH_FILE", "graph.json"),
        help="Path to graph JSON file (default: graph.json)"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload on code changes"
    )
    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()

    # Create configuration from arguments
    config = AppConfig(
        graph_file=args.graph_file,
        host=args.host,
        port=args.port,
    )

    # Print startup information
    print("=" * 60)
    print("Community Knowledge Graph Server")
    print("=" * 60)
    print(f"Graph file: {config.get_graph_path()}")
    print(f"Host: {config.host}")
    print(f"Port: {config.port}")
    print(f"API prefix: {config.api_prefix}")
    print(f"Web static path: {config.web_static_path}")
    print(f"Widget static path: {config.widget_static_path}")
    print("=" * 60)
    print()
    print("Endpoints:")
    print(f"  REST API:  http://{config.host}:{config.port}{config.api_prefix}")
    print(f"  MCP:       http://{config.host}:{config.port}/mcp")
    print(f"  Web App:   http://{config.host}:{config.port}/web")
    print(f"  Widget:    http://{config.host}:{config.port}/widget")
    print(f"  Health:    http://{config.host}:{config.port}/health")
    print("=" * 60)
    print()

    # Create app
    app = create_app(config)

    # Run with uvicorn
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
