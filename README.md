<h1>
  Juno MCP Server
  <img src="https://www.uptycs.com/hubfs/uptycs_logo_2C_on-light_rgb-1.svg" alt="Uptycs" height="32" align="right">
</h1>

MCP server for [Uptycs Juno](https://www.uptycs.com/juno-ai) — the AI-powered security assistant.

Connect Juno to any MCP-compatible client to investigate threats, analyze findings, and manage security investigations.

## How it works

```mermaid
flowchart LR
    Client["MCP Client"]
    Server["juno-mcp-server"]
    Juno["Uptycs Juno"]

    Client -- "tool calls" --> Server
    Server -- "responses" --> Client
    Server -- "HTTPS + JWT auth" --> Juno
    Juno -- "findings, recommendations, summaries" --> Server

    style Client fill:#4a90d9,stroke:#2c5f8a,color:#fff
    style Server fill:#2ecc71,stroke:#1a9c54,color:#fff
    style Juno fill:#e74c3c,stroke:#c0392b,color:#fff
```

1. The MCP client discovers available Juno tools via the MCP protocol
2. When a tool is called, the server authenticates with your Uptycs API key (JWT) and calls the Juno API
3. Juno processes the request and returns findings, summaries, and recommendations back through the server

```mermaid
sequenceDiagram
    participant Client as MCP Client
    participant Server as juno-mcp-server
    participant Juno as Uptycs Juno API

    Note over Client,Server: Connection setup
    Client->>Server: initialize
    Server-->>Client: available tools

    Note over Client,Juno: Investigation
    Client->>Server: create_investigation("privilege escalation attempts")
    activate Server
    Note over Server: Generate JWT from API key
    Server->>Juno: POST /investigations
    activate Juno
    Juno-->>Server: investigation + run ID
    deactivate Juno
    Server-->>Client: investigation created
    deactivate Server

    Client->>Server: get_run(investigation_id, run_id)
    activate Server
    Server->>Juno: GET /runs/{id}
    activate Juno
    Juno-->>Server: run with findings, tasks, summary
    deactivate Juno
    Server-->>Client: full run results
    deactivate Server

    Note over Client,Juno: Follow-up
    Client->>Server: create_follow_up("What accounts were involved?")
    activate Server
    Server->>Juno: POST /runs (parentRunId)
    activate Juno
    Juno-->>Server: new run
    deactivate Juno
    Server-->>Client: follow-up run created
    deactivate Server
```

## What you can do

- **Investigate threats** — "Are there any privilege escalation attempts in the last 24 hours?"
- **Analyze findings** — "Show me the findings and recommendations from that investigation"
- **Follow up** — "What user accounts were involved in the lateral movement?"
- **Manage investigations** — List, create, and delete investigations
- **Share** — Publish investigation runs for others to see

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- An Uptycs account with Juno enabled
- An Uptycs API key ([how to create one](https://docs.uptycs.com/articles/#!user-guide/api-access))

## Installation

```bash
git clone https://github.com/uptycslabs/juno-mcp-server.git
cd juno-mcp-server
```

### API key

Download your API key JSON file from the Uptycs console (**Configuration > API Keys**):

```json
{
  "key": "YOUR_API_KEY",
  "secret": "YOUR_API_SECRET",
  "customerId": "YOUR_CUSTOMER_ID",
  "domain": "your-domain",
  "domainSuffix": ".uptycs.net"
}
```

### Configure your MCP client

Add the following to your MCP client configuration. Example for Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "juno": {
      "command": "uv",
      "args": ["--directory", "/path/to/juno-mcp-server", "run", "juno-mcp"],
      "env": {
        "UPTYCS_API_KEY_FILE": "/path/to/apikey.json"
      }
    }
  }
}
```

Restart your MCP client. You should see Juno tools available.

## Tools

### Investigations

| Tool | Description |
|------|-------------|
| `create_investigation` | Start a new security investigation |
| `list_investigations` | List recent investigations |
| `get_investigation` | Get investigation details |
| `delete_investigation` | Delete an investigation |

### Runs & Follow-ups

| Tool | Description |
|------|-------------|
| `get_run` | Get investigation run results |
| `create_follow_up` | Ask a follow-up question on a completed run |

### Sharing

| Tool | Description |
|------|-------------|
| `publish_run` | Share a run with other users |
| `unpublish_run` | Unshare a run |
| `list_published_runs` | List shared runs |

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `UPTYCS_API_KEY_FILE` | Yes | — | Path to your Uptycs API key JSON file |

## License

Copyright [Uptycs, Inc.](https://uptycs.com/) All rights reserved.
