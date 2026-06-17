# SSPCloud Setup Guide

This guide explains how to get started with the Community Overview graph app in the SSPCloud environment.

## Prerequisites

- You have created an account on [SSPCloud](https://datalab.sspcloud.fr/).

## Step 1: Get Your API Key

1. Navigate to the **AI Chat** function at the top of SSPCloud, or go directly to <https://llm.lab.sspcloud.fr/>.
2. Click on your **name** in the bottom-left corner to open the menu.
3. Select **Settings**.
4. Go to **Account** and click **Show** next to **API Keys**.
5. Create a new API key (or view an existing one). This key allows you to use the models hosted on SSPCloud to power the AI assistant in the graph app.
6. **Copy** the API key.

## Step 2: Store Your Secrets

1. Go back to SSPCloud and navigate to **My Secrets**.
2. Create a new secret and name it **stockholmsecrets** (or a name of your choice).
3. Add the following variables to the secret:

   | Variable Name      | Value                              |
   | ------------------ | ---------------------------------- |
   | `OPENAI_API_KEY`   | *The API key you copied in Step 1* |
   | `OPENAI_BASE_URL`  | `https://llm.lab.sspcloud.fr/api`  |
   | `OPENAI_MODEL`     | `gemma4-26b-moe`                   |

4. Save the secret.

## Step 3: Launch the Service

### Option A: Quick Link (Recommended)

Use this pre-configured link to launch a VS Code Python service with all settings applied:

<https://datalab.sspcloud.fr/launcher/ide/vscode-python?name=stockholmsprint-graph&version=2.5.6&s3=default&vault.secret=«stockholmsecrets»&git.repository=«https%3A%2F%2Fgithub.com%2FAIML4OS%2FWP12_MetadataGraph%2F»&git.branch=«stockholmsprint»&networking.user.enabled=true&networking.user.ports[0]=8000&autoLaunch=true>

### Option B: Manual Setup

1. Go to **Service Catalog** in SSPCloud.
2. Find and start a **VS Code Python** service.
3. Configure the following settings:
   - **Vault** — set Secret to `stockholmsecrets`.
   - **Git** — set Repository to `https://github.com/jakobengdahl/CommunityOverview/` and Branch to `stockholmsprint`.
4. Launch the service.

## Step 4: Start the App

Once the VS Code service is running:

1. Open a terminal in VS Code and run:
   ```bash
   ./WP12_MetadataGraph/start-sprint.sh
   ```
   This script installs the required dependencies and starts the application.
2. When the app is ready, VS Code will indicate that a URL has been opened on **port 8000**. Click the link or open the forwarded port to access the app in your browser.

## Step 5 (optional): Connect to the graph via chatgpt, claude etc (MCP)

Once you know the service is running, you have the option to connect to the graph using an external ai-assistant or agent such as chatgpt, claude och openclaw. To connect to the running graph service:

1. Open SSPCloud and navigate to My Services where you will see your VS Code environment running.

2. Click on Open and under the headline Service Access, you'll see a text like this: "You can connect to your custom port (8000) using this link". Copy the link.

3. Open your ai-assistant and navigate to the settings where you can add MCP-servers (Customize -> Integration is claude, Settings -> Apps in ChatGPT).

4. Add the MCP-server link you copied from SSPCloud but add "/mcp/sse" at the end. Name your integration such as "metadata-graph". Select No Auth for authentication and connect. The MCP-server on the graph should now be possible to interact with in your AI-assistant by asking questions such as: What statistical programs are there in the metadata-graph
