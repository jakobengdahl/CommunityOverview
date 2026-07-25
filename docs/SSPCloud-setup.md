# SSPCloud Setup Guide

This guide explains how to get started with the Community Overview graph app in the SSPCloud environment, using the **stat-metadata** profile for European Statistical System metadata.

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
2. Create a new secret and name it **communityoverview-secrets** (or a name of your choice).
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

<https://datalab.sspcloud.fr/launcher/ide/vscode-python?name=communityoverview-stat-metadata&version=2.5.6&s3=default&vault.secret=«communityoverview-secrets»&git.repository=«https%3A%2F%2Fgithub.com%2FAIML4OS%2FWP12_MetadataGraph%2F»&git.branch=«dev»&networking.user.enabled=true&networking.user.ports[0]=8000&autoLaunch=true>

### Option B: Manual Setup

1. Go to **Service Catalog** in SSPCloud.
2. Find and start a **VS Code Python** service.
3. Configure the following settings:
   - **Vault** — set Secret to `communityoverview-secrets`.
   - **Git** — set Repository to `https://github.com/AIML4OS/WP12_MetadataGraph/` and Branch to `dev`.
4. Launch the service.

## Step 4: Start the App

Once the VS Code service is running:

1. Open a terminal in VS Code and run:
   ```bash
   cd WP12_MetadataGraph
   ./start-dev.sh --profile stat-metadata
   ```
   This installs the required dependencies, loads the **stat-metadata** profile (European Statistical System schema), and starts the application.

2. When the app is ready, VS Code will indicate that a URL has been opened on **port 8000**. Click the link or open the forwarded port to access the app in your browser.

## Step 5 (optional): Connect via MCP (ChatGPT, Claude, etc.)

Once the service is running, you can connect to it from an external AI assistant such as ChatGPT, Claude or Open WebUI.

1. In SSPCloud, navigate to **My Services** and find your VS Code environment.
2. Click **Open** — under **Service Access** you will see: *"You can connect to your custom port (8000) using this link"*. Copy that link.
3. Open your AI assistant's settings and find where to add MCP servers (e.g. *Customize → Integrations* in Claude, *Settings → Apps* in ChatGPT).
4. Add the copied link, appending `/mcp/sse` at the end. Name the integration e.g. `stat-metadata-graph`. Select **No Auth** for authentication and connect.

You can now interact with the graph directly from your AI assistant, for example:

> *"What statistical programmes are there in the metadata graph?"*  
> *"Show the data structure for the LFS dataset."*  
> *"Which NSIs are part of the ESS?"*

## Profile: stat-metadata

The `stat-metadata` profile pre-loads an ESS-focused schema with node types for:

- **Actor** — statistical offices (NSIs) and international organisations
- **StatisticalProgramme** — recurring statistical production activities (LFS, PPS, etc.)
- **DataSet** / **DataStructure** / **InstanceVariable** — dataset metadata chain
- **Concept** / **UnitType** / **CodeList** — semantic building blocks
- **Questionnaire** / **QuestionnaireComponent** / **ValueDomain** — data collection instruments
- **ProductionSolution** — technical pipelines and tools (with git repo link)
- **SubjectField** — thematic classification of programmes

The profile also includes expert agents for Metadata and ESS questions, and seed graph data for the European Statistical System.

To use the profile manually outside SSPCloud:

```bash
./start-dev.sh --profile stat-metadata
```
