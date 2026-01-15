#!/usr/bin/env python3
"""
Quick diagnostic script to check LLM provider configuration
Run this to debug provider setup issues
"""

import os
import sys
from dotenv import load_dotenv

print("=" * 60)
print("LLM Provider Configuration Check")
print("=" * 60)

# Load environment variables
load_dotenv()

# Check provider setting
provider = os.getenv("LLM_PROVIDER", "claude").lower()
print(f"\n1. Provider Configuration:")
print(f"   LLM_PROVIDER = '{provider}'")

if provider not in ["claude", "openai"]:
    print(f"   ⚠️  WARNING: Invalid provider '{provider}'")
    print(f"   Should be 'claude' or 'openai'")

# Check API keys
print(f"\n2. API Key Status:")

if provider == "openai":
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        print(f"   ✓ OPENAI_API_KEY is set")
        print(f"     Preview: {openai_key[:15]}...{openai_key[-4:]}")
    else:
        print(f"   ✗ OPENAI_API_KEY is NOT set")
        print(f"     You need to set this for OpenAI to work!")

    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if anthropic_key:
        print(f"   ℹ️  ANTHROPIC_API_KEY is also set (not used)")

else:  # claude
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if anthropic_key:
        print(f"   ✓ ANTHROPIC_API_KEY is set")
        print(f"     Preview: {anthropic_key[:15]}...{anthropic_key[-4:]}")
    else:
        print(f"   ✗ ANTHROPIC_API_KEY is NOT set")
        print(f"     You need to set this for Claude to work!")

    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        print(f"   ℹ️  OPENAI_API_KEY is also set (not used)")

# Check module imports
print(f"\n3. SDK Installation:")

try:
    import anthropic
    print(f"   ✓ anthropic SDK installed (version {anthropic.__version__})")
except ImportError:
    print(f"   ✗ anthropic SDK not installed")
    if provider == "claude":
        print(f"     Install with: pip install anthropic>=0.18.0")

try:
    import openai
    print(f"   ✓ openai SDK installed (version {openai.__version__})")
except ImportError:
    print(f"   ✗ openai SDK not installed")
    if provider == "openai":
        print(f"     Install with: pip install openai>=1.0.0")

# Check provider creation
print(f"\n4. Provider Creation Test:")

try:
    sys.path.insert(0, os.path.dirname(__file__))
    from llm_providers import create_provider

    if provider == "openai" and openai_key:
        test_provider = create_provider(openai_key[:20], "openai")
        print(f"   ✓ OpenAI provider created successfully")
        print(f"     Model: {test_provider.model}")
    elif provider == "claude" and anthropic_key:
        test_provider = create_provider(anthropic_key[:20], "claude")
        print(f"   ✓ Claude provider created successfully")
        print(f"     Model: {test_provider.model}")
    else:
        print(f"   ⚠️  Cannot test provider creation - API key missing")

except Exception as e:
    print(f"   ✗ Provider creation failed: {e}")

# Summary
print(f"\n" + "=" * 60)
print("Summary:")
print("=" * 60)

if provider == "openai":
    if openai_key:
        print("✓ Configuration looks good for OpenAI")
        print("\nTo start server with OpenAI:")
        print("  export LLM_PROVIDER=openai")
        print("  export OPENAI_API_KEY=your-key")
        print("  python server.py")
    else:
        print("✗ OpenAI API key is missing!")
        print("\nTo fix:")
        print("  export OPENAI_API_KEY=your-openai-key")
        print("  python server.py")
elif provider == "claude":
    if anthropic_key:
        print("✓ Configuration looks good for Claude")
        print("\nTo start server with Claude:")
        print("  export LLM_PROVIDER=claude")
        print("  export ANTHROPIC_API_KEY=your-key")
        print("  python server.py")
    else:
        print("✗ Anthropic API key is missing!")
        print("\nTo fix:")
        print("  export ANTHROPIC_API_KEY=your-anthropic-key")
        print("  python server.py")

print("\nFor more help, see: TROUBLESHOOTING_OPENAI.md")
print("=" * 60)
