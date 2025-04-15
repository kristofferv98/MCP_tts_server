# MCP TTS Server

A versatile TTS (Text-to-Speech) server built on the Model Context Protocol (MCP) framework. This server provides access to multiple TTS engines through a unified interface:

1. **Kokoro TTS** - High-quality local TTS engine
2. **OpenAI TTS** - Cloud-based TTS via OpenAI's API

## Features

- 🌐 Multiple TTS engines in one unified server
- 🎧 Real-time streaming audio playback
- 🔄 MCP protocol support for seamless integration with Claude and other LLMs
- 🎛️ Configurable voice selection for both engines
- 💬 Support for voice customization via natural language instructions (OpenAI)
- ⚡ Speed adjustment for both TTS engines
- 🛑 Playback control for stopping audio and clearing the queue

## Installation

### Prerequisites

- Python 3.10 or higher
- [uv](https://github.com/astral-sh/uv) package manager
- OpenAI API key (for OpenAI TTS functionality)

### Quick Install

```bash
# Clone the repository
git clone https://github.com/kristofferv98/MCP_tts_server.git
cd MCP_tts_server

# Create a virtual environment and install dependencies
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
uv pip install -e .
```

### Configuration

Create a `.env` file based on the provided `.env.example`:

```bash
cp .env.example .env
```

Edit the `.env` file to add your OpenAI API key:

```
OPENAI_API_KEY=your_openai_api_key_here
```

## Integration with Claude Desktop

To use this server with Claude Desktop:

1. Install the server:
   ```bash
   fastmcp install ./tts_mcp.py --name tts
   ```

2. Alternatively, you can manually add the server to Claude Desktop's configuration file:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`

   Add this entry to the `mcpServers` section:

   ```json
   "kokoro_tts": {
     "command": "uv",
     "args": [
       "--directory",
       "/path/to/MCP_tts_server",
       "run",
       "tts_mcp.py"
     ]
   }
   ```

   Example configuration using the full path to uv:

   ```json
   "kokoro_tts": {
     "command": "/Users/username/.local/bin/uv",
     "args": [
       "--directory",
       "/Users/username/Documents/MCP_Servers/MCP_tts_server",
       "run",
       "tts_mcp.py"
     ]
   }
   ```

## MCP Function Definitions

The server exposes the following MCP tools:

### Main TTS Function

```json
{
  "description": "Convert text to speech using the preferred engine and streams the speech to the user. The base voice for the AI is the Kokoro engine, to keep AI's personality consistent. This unified function provides access to both Kokoro TTS (local) and OpenAI TTS (cloud API).",
  "name": "tts",
  "parameters": {
    "properties": {
      "text": {"title": "Text", "type": "string"},
      "engine": {"default": "kokoro", "title": "Engine", "type": "string"},
      "speed": {"default": 1, "title": "Speed", "type": "number"},
      "voice": {"default": "", "title": "Voice", "type": "string"},
      "instructions": {"default": "", "title": "Instructions", "type": "string"}
    },
    "required": ["text"]
  }
}
```

#### Parameters:

- **text** (required): Text to convert to speech
- **engine** (optional): TTS engine to use - "kokoro" (default, local) or "openai" (cloud)
- **speed** (optional): Playback speed (0.8-1.5 typical)
- **voice** (optional): Voice name to use (engine-specific)
- **instructions** (optional): Voice customization instructions for OpenAI TTS

### Stop Playback Function

```json
{
  "description": "Stops the currently playing audio (if any) and clears all pending TTS requests from the queue. Relies on the background worker detecting the cancellation signal.",
  "name": "tts_stop_playback_and_clear_queue",
  "parameters": {
    "properties": {}
  }
}
```

### Voice Examples Function

```json
{
  "description": "Provides research-based examples of effective voice instructions for OpenAI TTS.",
  "name": "tts_examples",
  "parameters": {
    "properties": {
      "category": {"default": "general", "title": "Category", "type": "string"}
    }
  }
}
```

#### Categories:
- general
- accents
- characters
- emotions
- narration

### Get TTS Instructions Function

```json
{
  "description": "Fetches TTS instructions by calling get_voice_info.",
  "name": "get_tts_instructions",
  "parameters": {
    "properties": {}
  }
}
```

## Direct Usage

The primary way to use this server is through Claude Desktop or other MCP supported integration as described above. However, you can also run the server directly for testing purposes:

```bash
# Run with the uv environment manager
uv run python tts_mcp.py
```

This will start the MCP server, making it available for connection.

## Available Voices

### Kokoro TTS
- Default voice: `af_heart`

### OpenAI TTS
- Available voices: `alloy`, `ash`, `ballad`, `coral`, `echo`, `fable`, `onyx`, `nova`, `sage`, `shimmer`
- Default model: `gpt-4o-mini-tts`

## Development and Testing

To test the server locally during development:

```bash
fastmcp dev ./tts_mcp.py
```

This will start the MCP Inspector interface where you can test the server's functionality.

## Implementation Details

The server is implemented using FastMCP and follows best practices for MCP server development:

- **Unified Interface**: A single function supports both Kokoro and OpenAI engines
- **Streaming Support**: Audio is streamed directly to the client when possible
- **Fallback Mechanisms**: File-based playback when streaming isn't available
- **Voice Customization**: Support for natural language instructions with OpenAI TTS
- **Lifespan Management**: Proper initialization and cleanup of resources

## Troubleshooting

- **No Audio Output**: Check your system's audio configuration
- **OpenAI TTS Failures**: Verify your API key is valid and has TTS access permissions
- **Server Not Found**: Make sure the MCP server is correctly registered in your MCP host

## License

This project is licensed under the Apache License 2.0 - see the LICENSE file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. 
