# Kokoro TTS CLI

A simple yet powerful command-line interface for the Kokoro Text-to-Speech system.

## Features

- 💬 High-quality text-to-speech generation using the Kokoro engine
- 🔄 Real-time streaming playback as speech segments are generated
- 🎵 Multiple output formats (WAV and MP3)
- 🔌 Cross-platform audio playback (macOS, Linux, Windows)
- 🧵 Async/await implementation for better performance
- 🚀 Automatic dependency installation
- 📦 Single self-contained file

## Installation

### Prerequisites

- Python 3.10 or higher
- [uv](https://github.com/astral-sh/uv) package manager
- ffmpeg (for MP3 conversion)

### Quick Install

```bash
# Clone the repository
git clone https://github.com/yourusername/kokoro-tts-cli.git
cd kokoro-tts-cli

# Create a virtual environment and install dependencies
uv venv
uv pip install -r requirements.txt
```

## Usage

Run the script with `uv run`:

```bash
# Basic usage
uv run speak.py "Hello, this is a text-to-speech test."

# With custom voice and speed
uv run speak.py "Hello, this is a test." --voice af_heart --speed 1.2

# Generate MP3 audio
uv run speak.py "Hello, this is a test." --format mp3

# Save to a specific file
uv run speak.py "Hello, this is a test." --output my_speech.wav

# Generate audio without playing it
uv run speak.py "Hello, this is a test." --no-play

# Disable streaming (wait until all segments are generated before playing)
uv run speak.py "A very long text to convert to speech..." --no-stream
```

## Options

- `--voice VOICE` - Voice to use (default: af_heart)
- `--speed SPEED` - Speech speed (default: 1.0)
- `--format FORMAT` - Output format: wav or mp3 (default: wav)
- `--output FILE` - Save to a specific output file instead of a temporary file
- `--no-play` - Generate the audio file without playing it
- `--no-stream` - Don't stream audio as it's generated (wait until all is ready)

## Streaming vs Non-Streaming Mode

### Streaming Mode (Default)
In streaming mode, the system plays each segment of audio as soon as it's generated, providing immediate feedback and a better experience for longer texts.

### Non-Streaming Mode
In non-streaming mode (`--no-stream`), the system waits until all segments are generated before playing the combined audio file. This can result in a more polished output without any potential gaps between segments.

## Project Structure

- `speak.py` - Main script containing the TTS functionality
- `requirements.txt` - List of Python dependencies
- `LICENSE` - MIT License
- `.gitignore` - Standard Python gitignore file

## Dependencies

The application uses:
- `kokoro` - For text-to-speech generation
- `numpy` and `soundfile` - For audio processing
- `termcolor` - For colorized terminal output

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

# MCP TTS Server

A versatile TTS (Text-to-Speech) server built on the MCP (Multi-model Command Protocol) framework. This server provides access to multiple TTS engines:

1. **Kokoro TTS** - High-quality local TTS engine
2. **OpenAI TTS** - Cloud-based TTS via OpenAI's API

## Features

- 🌐 Multiple TTS engines in one server
- 🎧 Real-time streaming audio playback while generating
- 🔄 MCP protocol support for easy integration with various clients
- 🎛️ Configurable voice selection for both engines
- 💬 Support for voice customization via instructions (OpenAI)
- ⚡ Speed adjustment for local Kokoro TTS

## Installation

### Prerequisites

- Python 3.10 or higher
- [uv](https://github.com/astral-sh/uv) package manager

### Quick Install

```bash
# Clone the repository
git clone https://github.com/yourusername/mcp_tts_server.git
cd mcp_tts_server

# Create a virtual environment and install dependencies
uv venv
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

## Usage

Run the MCP server:

```bash
# Using Python directly
python tts_mcp.py

# Or using uv to run with proper environment
uv run python tts_mcp.py
```

The server exposes two main tools:

1. `speak` - Uses Kokoro TTS (local engine)
2. `openai_speak` - Uses OpenAI TTS (cloud API)

### Client Integration

You can connect to this MCP server using any MCP client. Example:

```python
from mcp.client import MCPClient

async def main():
    client = MCPClient()
    await client.connect("mcp://127.0.0.1:8000")  # Replace with your server address
    
    # Use Kokoro TTS
    await client.call("speak", text="Hello, this is Kokoro TTS.", speed=1.2)
    
    # Use OpenAI TTS
    await client.call(
        "openai_speak", 
        text="Hello, this is OpenAI TTS.", 
        voice="nova",
        instructions="Speak with enthusiasm"
    )
```

## Available Voices

### Kokoro TTS
- Default voice: `af_heart`

### OpenAI TTS
- Available voices: `alloy`, `ash`, `ballad`, `coral`, `echo`, `fable`, `onyx`, `nova`, `sage`, `shimmer`
- Default model: `gpt-4o-mini-tts`

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. 