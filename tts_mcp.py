#!/usr/bin/env python3
"""
MCP Server wrapper for TTS engines (Async Queued Streaming Playback - v3.1 Kokoro Fix)

- Fixes AttributeError and UnboundLocalError from previous versions.
- Corrects OpenAI API parameters (model, voice, format).
- TTS requests are added to a queue.
- A background worker processes the queue sequentially.
- Audio playback starts streaming using ffplay.
- Server remains responsive during TTS generation/playback.
- Provides a cancellation mechanism to stop current playback and clear the queue.
- **v3.1 Fix:** Ensures Kokoro playback responds promptly to cancellation requests.

Requires: ffplay (from FFmpeg) installed and in PATH.
"""
import sys
import os
import asyncio
import tempfile
import time
from pathlib import Path
from datetime import datetime
import subprocess
import traceback
from typing import Optional, Dict, Any, AsyncGenerator, Tuple
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from io import BytesIO
import numpy as np

# Use AnyIO for cross-platform async operations
import anyio
# Need this for run_sync
from anyio.to_thread import run_sync as anyio_run_sync

# Function to print to stderr
def log(message, color=None):
    try:
        print(message, file=sys.stderr, flush=True)
    except (ValueError, IOError):
        pass # Handle case where stderr is closed

# Import MCP server and Context
try:
    from mcp.server.fastmcp import FastMCP, Context
except ImportError:
    log("Error: MCP server package not found.")
    log("Please install it using: pip install 'mcp[cli]>=1.4.0'")
    sys.exit(1)

# Termcolor (optional)
try:
    from termcolor import colored
    def colored_log(message, color="white"):
        try:
            log(colored(message, color))
        except (ValueError, IOError, AttributeError):
            log(message) # Fallback if termcolor fails or streams closed
except ImportError:
    def colored_log(message, color="white"):
        log(message)

# --- Kokoro TTS Import Logic ---
try:
    from speak import KokoroTTS
    colored_log("Successfully imported KokoroTTS class", "green")
except ImportError as e:
    colored_log(f"Error importing KokoroTTS: {e}", "red")
    KokoroTTS = None
# --- End Kokoro TTS Import Logic ---

# --- OpenAI and Environment Variable Logic ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_AVAILABLE = False
if OPENAI_API_KEY:
    try:
        from openai import AsyncOpenAI, OpenAI
        try:
            # Try to import LocalAudioPlayer - needed for true streaming
            from openai.helpers import LocalAudioPlayer
            # Check if LocalAudioPlayer supports cancellation_check (added in later versions)
            import inspect
            sig = inspect.signature(LocalAudioPlayer.play)
            AUDIO_STREAMING_AVAILABLE = 'cancellation_check' in sig.parameters
            if not AUDIO_STREAMING_AVAILABLE:
                 colored_log("Warning: Installed openai LocalAudioPlayer doesn't support 'cancellation_check'. True streaming cancellation may not work.", "yellow")
        except ImportError:
            colored_log("Warning: openai[voice_helpers] not installed. Will use fallback method.", "yellow")
            colored_log("For true streaming, install: pip install 'openai[voice_helpers]>=1.23.0'", "yellow") # Version supporting cancellation_check
            AUDIO_STREAMING_AVAILABLE = False
        OPENAI_AVAILABLE = True
        colored_log("Successfully imported OpenAI packages", "green")
    except ImportError:
        colored_log("OpenAI package not found...", "yellow")
else:
     colored_log("Warning: No OpenAI API key found...", "yellow")
# --- End OpenAI Logic ---


# --- Playback Function (Using asyncio directly now) ---
async def play_audio_stream(audio_stream: AsyncGenerator[bytes, None], task_id: str) -> Tuple[Optional[asyncio.subprocess.Process], Optional[asyncio.Task], Optional[asyncio.Task]]:
    """Plays audio via ffplay, returns handles or Nones."""
    player_cmd = "ffplay"
    try:
        process_check = await asyncio.create_subprocess_exec(
            player_cmd, "-version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await process_check.wait()
        if process_check.returncode != 0:
            raise FileNotFoundError(f"'{player_cmd}' check failed with code {process_check.returncode}")
    except (FileNotFoundError, OSError) as check_err:
        colored_log(f"Error: '{player_cmd}' command not found or failed check ({check_err}). Cannot play audio.", "red")
        return None, None, None

    colored_log(f"[Playback {task_id}] Starting ffplay...", "blue")
    process: Optional[asyncio.subprocess.Process] = None
    pipe_task: Optional[asyncio.Task] = None
    stderr_task: Optional[asyncio.Task] = None
    try:
        process = await asyncio.create_subprocess_exec(
            player_cmd,
            "-nodisp", "-hide_banner", "-autoexit",
            "-loglevel", "error",
            "-i", "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _read_stderr():
            """Read stderr from ffplay."""
            if process and process.stderr:
                 while True:
                    try:
                        line = await process.stderr.readline()
                        if not line: break
                        line_str = line.decode(errors='ignore').strip()
                        if line_str:
                             colored_log(f"[ffplay stderr {task_id}] {line_str}", "grey")
                    except asyncio.CancelledError: break
                    except Exception as e:
                         colored_log(f"[ffplay stderr {task_id}] Error reading: {e}", "red")
                         break

        stderr_task = asyncio.create_task(_read_stderr(), name=f"stderr_{task_id}")

        async def _pipe_data():
            """Pipe audio data to process stdin."""
            stream_yielded_data = False
            bytes_piped = 0
            try:
                async for chunk in audio_stream:
                    stream_yielded_data = True
                    if process and process.stdin and not process.stdin.is_closing():
                        try:
                             process.stdin.write(chunk)
                             await process.stdin.drain()
                             bytes_piped += len(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                             colored_log(f"[Playback {task_id}] Player stdin pipe broken after {bytes_piped} bytes.", "yellow")
                             break
                        except Exception as write_e:
                             colored_log(f"[Playback {task_id}] Error writing to player stdin: {write_e}", "red")
                             break
                    else:
                        colored_log(f"[Playback {task_id}] Player stdin unavailable.", "yellow")
                        break
                if not stream_yielded_data:
                     colored_log(f"[Playback {task_id}] Audio stream yielded no data.", "yellow")
                else:
                     colored_log(f"[Playback {task_id}] Finished piping {bytes_piped} bytes.", "blue")
            except asyncio.CancelledError:
                 colored_log(f"[Playback {task_id}] Audio piping cancelled.", "yellow")
            except Exception as e:
                 colored_log(f"[Playback {task_id}] Error piping audio data: {e}", "red")
                 traceback.print_exc(file=sys.stderr)
            finally:
                 if process and process.stdin and not process.stdin.is_closing():
                    try: process.stdin.close()
                    except (BrokenPipeError, ConnectionResetError): pass
                    except Exception as close_e:
                        colored_log(f"[Playback {task_id}] Error closing player stdin: {close_e}", "red")

        pipe_task = asyncio.create_task(_pipe_data(), name=f"pipe_{task_id}")

        return process, pipe_task, stderr_task

    except Exception as e:
        colored_log(f"[Playback {task_id}] Failed to start audio player: {e}", "red")
        if process and process.returncode is None:
            try: process.terminate()
            except ProcessLookupError: pass
        if pipe_task and not pipe_task.done(): pipe_task.cancel()
        if stderr_task and not stderr_task.done(): stderr_task.cancel()
        return None, None, None
# --- End Playback Function ---


# --- TTS Engine Classes ---
class OpenAITTS:
    def __init__(self, api_key=None):
        if not OPENAI_AVAILABLE: raise ImportError("OpenAI unavailable")
        self.api_key = api_key or OPENAI_API_KEY
        if not self.api_key: raise ValueError("No OpenAI API key")
        try:
            self.client = OpenAI(api_key=self.api_key)
            self.async_client = AsyncOpenAI(api_key=self.api_key)
            self.valid_voices = ["alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer", "verse"]
            self.valid_formats = ["mp3", "opus", "aac", "flac", "wav", "pcm"]
            self.default_voice = "ash"
            self.default_format = "mp3"
            self.streaming_format = "wav"
            self.default_model = "gpt-4o-mini-tts" # Changed from gpt-4o-mini
            if AUDIO_STREAMING_AVAILABLE:
                self.audio_player = LocalAudioPlayer()
                colored_log("LocalAudioPlayer available - will use true streaming", "green")
            colored_log(f"OpenAI TTS client initialized (Model: {self.default_model})", "green")
        except Exception as e:
            colored_log(f"Error initializing OpenAI client: {e}", "red")
            raise

    async def stream_and_play(self, text, voice=None, instructions=None, speed=1.0, cancel_event=None):
        """Generate speech and stream it for immediate playback."""
        voice = voice if voice in self.valid_voices else self.default_voice
        colored_log(f"Generating speech for: {text[:100]}{'...' if len(text) > 100 else ''}", "cyan")
        colored_log(f"Voice: {voice}, Speed: {speed}", "cyan")
        if instructions: colored_log(f"Instructions: {instructions}", "cyan")
        start_time = time.time()
        params = {"model": self.default_model, "voice": voice, "input": text}
        if instructions: params["instructions"] = instructions
        if speed != 1.0: params["speed"] = max(0.25, min(4.0, speed))

        if cancel_event and cancel_event.is_set():
            colored_log("OpenAI TTS cancelled before starting", "yellow")
            return False

        if AUDIO_STREAMING_AVAILABLE:
            try:
                colored_log("Using true streaming with LocalAudioPlayer - playback will start immediately", "cyan")
                params["response_format"] = self.streaming_format

                async with self.async_client.audio.speech.with_streaming_response.create(**params) as response:
                    def cancellation_check():
                        if cancel_event and cancel_event.is_set():
                            colored_log("OpenAI TTS playback cancelled during streaming (via check)", "yellow")
                            return True
                        return False
                    # Pass cancellation check *only if supported*
                    play_kwargs = {}
                    if 'cancellation_check' in inspect.signature(self.audio_player.play).parameters:
                         play_kwargs['cancellation_check'] = cancellation_check
                    else:
                         colored_log("Note: LocalAudioPlayer doesn't support cancellation_check, relying on worker cancellation.", "grey")

                    await self.audio_player.play(response, **play_kwargs)

                    if cancel_event and cancel_event.is_set():
                        colored_log("OpenAI TTS true streaming playback was cancelled", "yellow")
                        return False # Indicate cancellation

                elapsed = time.time() - start_time
                colored_log(f"True streaming playback completed in {elapsed:.2f}s", "green")
                return True
            except Exception as streaming_error:
                # Check if cancellation happened during setup or playback itself
                if cancel_event and cancel_event.is_set():
                     colored_log(f"OpenAI TTS true streaming cancelled during operation: {streaming_error}", "yellow")
                     return False # Indicate cancellation

                colored_log(f"True streaming failed: {str(streaming_error)}", "yellow")
                colored_log("Falling back to generate-then-play approach...", "yellow")

        # Fallback generate-then-play
        temp_file = None
        try:
            temp_dir = tempfile.gettempdir()
            temp_file = os.path.join(temp_dir, f"openai_tts_{int(time.time() * 1000)}.mp3")
            params["response_format"] = self.default_format

            if cancel_event and cancel_event.is_set():
                colored_log("OpenAI TTS cancelled before generation (fallback)", "yellow")
                return False

            colored_log("Using generate-then-play approach (not true streaming)", "yellow")
            response = await self.async_client.audio.speech.create(**params)
            await response.astream_to_file(temp_file) # Note: Deprecation warning likely here
            elapsed_generation = time.time() - start_time
            colored_log(f"Speech generation completed in {elapsed_generation:.2f}s", "green")

            if cancel_event and cancel_event.is_set():
                colored_log("OpenAI TTS cancelled before playback (fallback)", "yellow")
                return False

            playback_start = time.time()
            player_process = None
            try:
                 cmd = []
                 if sys.platform == "darwin": cmd = ["afplay", temp_file]
                 elif sys.platform == "linux": cmd = ["aplay", temp_file]
                 elif sys.platform == "win32":
                     # Simple fire-and-forget on Windows fallback
                     subprocess.run(["start", "/MIN", temp_file], shell=True, check=True)
                     colored_log("Started Windows playback (fire-and-forget)", "blue")
                     # Assume success on Windows for now, can't easily track/cancel
                     return True
                 else:
                     colored_log(f"Unsupported platform for fallback playback: {sys.platform}. Saved to {temp_file}", "yellow")
                     return False # Can't play

                 if cmd: # Only run Popen if we have a command
                     player_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                     colored_log(f"Started fallback player process {player_process.pid}", "blue")
                     # Wait for playback with cancellation check
                     while player_process.poll() is None:
                         await asyncio.sleep(0.1)
                         if cancel_event and cancel_event.is_set():
                             colored_log(f"OpenAI TTS fallback playback cancelled via event check (PID: {player_process.pid})", "yellow")
                             player_process.terminate()
                             try:
                                 await asyncio.to_thread.run_sync(player_process.wait, timeout=0.5)
                             except TimeoutError:
                                 player_process.kill()
                             except Exception: pass
                             return False # Indicate cancellation
                     # Check return code after normal exit
                     if player_process.returncode != 0:
                          stderr_bytes = player_process.stderr.read() if player_process.stderr else b''
                          colored_log(f"Fallback player exited with code {player_process.returncode}. Stderr: {stderr_bytes.decode(errors='ignore')}", "yellow")
                          # Don't necessarily fail the whole TTS request for player error, just log it.

            except FileNotFoundError:
                 colored_log(f"Error: Fallback audio player command not found ('{cmd[0]}').", "red")
                 return False
            except Exception as play_error:
                colored_log(f"Error during fallback playback: {str(play_error)}", "red")
                if player_process and player_process.poll() is None:
                    try: player_process.kill()
                    except Exception: pass
                return False # Indicate failure

            elapsed_playback = time.time() - playback_start
            colored_log(f"Fallback audio playback completed in {elapsed_playback:.2f}s", "green")
            return True # Indicate success
        except Exception as e:
            if cancel_event and cancel_event.is_set():
                 colored_log(f"OpenAI TTS cancelled during fallback operation: {e}", "yellow")
                 return False
            error_message = str(e)
            colored_log(f"Error generating/playing speech (fallback): {error_message}", "red")
            traceback.print_exc(file=sys.stderr)
            return False # Indicate failure
        finally:
             # Clean up temp file
             if temp_file and os.path.exists(temp_file):
                 try: await anyio.to_thread.run_sync(os.unlink, temp_file)
                 except Exception as e: colored_log(f"Warning: Could not clean up temp file {temp_file}: {e}", "yellow")


class KokoroTTSWrapper:
    """Wrapper for KokoroTTS."""
    def __init__(self):
        self.kokoro = None
        if KokoroTTS:
            try:
                self.kokoro = KokoroTTS()
                colored_log("KokoroTTS wrapper initialized.", "green")
            except Exception as e:
                colored_log(f"Error initializing KokoroTTS instance: {e}", "red")
        else:
            colored_log("KokoroTTS unavailable, wrapper disabled.", "yellow")

    async def get_audio_stream(self, text: str, speed: float) -> AsyncGenerator[bytes, None]:
        """Gets audio stream from Kokoro using true segment-by-segment streaming."""
        if not self.kokoro:
             colored_log("[Kokoro] Engine not initialized.", "red")
             return

        try:
            colored_log("[Kokoro] Using true segment-by-segment streaming.", "green")
            sample_rate = 24000; channels = 1; bits_per_sample = 16
            header = BytesIO()
            header.write(b'RIFF'); header.write((2**31 - 1).to_bytes(4, 'little')); header.write(b'WAVE')
            header.write(b'fmt '); header.write((16).to_bytes(4, 'little')); header.write((1).to_bytes(2, 'little'))
            header.write((channels).to_bytes(2, 'little')); header.write((sample_rate).to_bytes(4, 'little'))
            header.write((sample_rate * channels * bits_per_sample // 8).to_bytes(4, 'little'))
            header.write((channels * bits_per_sample // 8).to_bytes(2, 'little')); header.write((bits_per_sample).to_bytes(2, 'little'))
            header.write(b'data'); header.write((2**31 - 1).to_bytes(4, 'little'))
            header.seek(0)
            yield header.read()

            segment_count = 0
            colored_log("[Kokoro] Starting segment generation...", "cyan")

            # Check for direct pipeline access first (preferred)
            if hasattr(self.kokoro, 'pipeline') and callable(getattr(self.kokoro, 'pipeline', None)):
                pipeline = self.kokoro.pipeline
                generator = pipeline(
                    text, voice="af_heart", speed=speed, split_pattern=r'[.!?]\s+'
                )
                for i, (graphemes, phonemes, audio) in enumerate(generator):
                    if len(audio) == 0: continue
                    if not isinstance(audio, np.ndarray): audio = np.array(audio, dtype=np.float32)
                    segment_count += 1
                    # colored_log(f"[Kokoro] Yielding segment {segment_count}", "grey") # Optional: noisy
                    yield (audio * 32767).astype(np.int16).tobytes()
                colored_log(f"[Kokoro] Finished streaming {segment_count} segments (pipeline).", "blue")

            # Fallback to older segment generation method if pipeline not available/suitable
            elif hasattr(self.kokoro, '_generate_segments'):
                colored_log("[Kokoro] Using _generate_segments fallback.", "yellow")
                # This might be sync, run in thread if necessary - assuming it yields for now
                for audio, sr in self.kokoro._generate_segments(text, voice="af_heart", speed=speed):
                     if len(audio) == 0: continue
                     if sr != sample_rate: colored_log(f"[Kokoro] Warning: Sample rate mismatch {sr} != {sample_rate}", "yellow")
                     segment_count += 1
                     # colored_log(f"[Kokoro] Yielding segment {segment_count}", "grey")
                     yield (audio * 32767).astype(np.int16).tobytes()
                colored_log(f"[Kokoro] Finished streaming {segment_count} segments (fallback).", "blue")
            else:
                 colored_log("[Kokoro] No known streaming generation method found!", "red")
                 raise NotImplementedError("KokoroTTS instance lacks streaming capability.")

        except asyncio.CancelledError:
             colored_log("[Kokoro] Audio stream generation cancelled.", "yellow")
             raise # Propagate cancellation
        except Exception as e:
            colored_log(f"[Kokoro] Error in get_audio_stream: {e}", "red")
            traceback.print_exc(file=sys.stderr)
            # Don't fallback here, let the worker handle the error
# --- End TTS Engine Classes ---


# --- Queue and Worker Setup ---
@dataclass
class TTSRequest:
    task_id: str
    mcp_request_id: Any
    text: str
    engine: str
    params: Dict[str, Any]
    future: asyncio.Future = field(default_factory=asyncio.Future)

# Global state (managed by lifespan)
tts_queue: Optional[asyncio.Queue[Optional[TTSRequest]]] = None
playback_worker_task: Optional[asyncio.Task] = None
current_playback_info: Dict[str, Any] = {}
playback_lock = asyncio.Lock()
tts_cancel_requested = asyncio.Event() # Global cancellation signal

async def playback_worker():
    """Background task processing the TTS queue."""
    global tts_queue, current_playback_info, tts_cancel_requested
    if tts_queue is None: return

    colored_log("[Worker] Playback worker started.", "green")
    while True:
        request: Optional[TTSRequest] = None
        acquired_lock = False
        task_future: Optional[asyncio.Future] = None
        process_wait_task: Optional[asyncio.Task] = None # MODIFIED: Moved out
        cancel_wait_task: Optional[asyncio.Task] = None # MODIFIED: Moved out

        try:
            colored_log(f"[Worker] Queue size: {tts_queue.qsize()}. Waiting...", "grey")
            request = await tts_queue.get()

            if request is None: break # Shutdown signal

            task_future = request.future
            colored_log(f"[Worker] Processing task: {request.task_id} (MCP ID: {request.mcp_request_id})", "blue")
            tts_cancel_requested.clear() # Clear flag for new request

            await playback_lock.acquire()
            acquired_lock = True
            current_playback_info = { # Reset and store current task info
                "task_id": request.task_id, "engine": request.engine,
                "process": None, "pipe_task": None, "stderr_task": None,
                "future": task_future, "openai_tracking": None
            }

            try:
                # --- Process based on engine type ---
                if request.engine == "kokoro" and kokoro_tts_wrapper:
                    audio_stream = kokoro_tts_wrapper.get_audio_stream(request.text, request.params.get("speed", 1.0))
                    if audio_stream:
                        playback_result = await play_audio_stream(audio_stream, request.task_id)
                        if playback_result:
                            process, pipe_task, stderr_task = playback_result
                            if process:
                                current_playback_info.update({
                                    "process": process, "pipe_task": pipe_task, "stderr_task": stderr_task
                                })
                                colored_log(f"[Worker] Waiting for Kokoro PID {process.pid} OR cancellation (Task {request.task_id})...", "blue")

                                # --- KOKORO CANCELLATION HANDLING ---
                                process_wait_task = asyncio.create_task(process.wait(), name=f"wait_{request.task_id}")
                                cancel_wait_task = asyncio.create_task(tts_cancel_requested.wait(), name=f"cancel_wait_{request.task_id}")

                                done, pending = await asyncio.wait(
                                    [process_wait_task, cancel_wait_task],
                                    return_when=asyncio.FIRST_COMPLETED
                                )

                                was_cancelled = False
                                if cancel_wait_task in done:
                                    colored_log(f"[Worker] Cancellation detected for Kokoro Task {request.task_id}. Terminating PID {process.pid}.", "yellow")
                                    was_cancelled = True
                                    if process.returncode is None:
                                        try:
                                            process.terminate()
                                            await asyncio.wait_for(process.wait(), timeout=0.5) # Brief wait for clean termination
                                        except asyncio.TimeoutError: process.kill()
                                        except ProcessLookupError: pass
                                        except Exception as term_err: colored_log(f"Error terminating PID {process.pid}: {term_err}", "red")
                                    if process_wait_task in pending: process_wait_task.cancel()

                                return_code = None
                                try:
                                     if process_wait_task in done or was_cancelled: # Await even if cancelled to retrieve exception/result
                                         return_code = await process_wait_task
                                except asyncio.CancelledError:
                                     if not was_cancelled: # Only log if externally cancelled, not by our event logic
                                           colored_log(f"[Worker] Kokoro process wait task cancelled externally for {request.task_id}.", "yellow")
                                     pass # Expected if cancel_wait_task won
                                except Exception as wait_err:
                                     colored_log(f"[Worker] Error awaiting process task for {request.task_id}: {wait_err}", "red")

                                # Cleanup the cancel wait task if it didn't complete
                                if cancel_wait_task in pending: cancel_wait_task.cancel()

                                if was_cancelled:
                                     raise asyncio.CancelledError("Kokoro playback cancelled by event")

                                colored_log(f"[Worker] Kokoro PID {process.pid} finished with code {return_code} (Task {request.task_id}).", "blue")
                                if return_code != 0 and return_code is not None: # Check if failed
                                    stderr_output = ""
                                    if process.stderr:
                                        try: stderr_output = (await process.stderr.read()).decode(errors='ignore')
                                        except Exception: pass
                                    raise RuntimeError(f"Kokoro audio player exited with {return_code}. Stderr: {stderr_output.strip()}")
                                # --- END KOKORO CANCELLATION ---
                            else: raise RuntimeError("Kokoro playback failed to init process.")
                        else: raise RuntimeError("Failed to start Kokoro player stream.")
                    else: raise RuntimeError("Failed to get Kokoro audio stream.")

                elif request.engine == "openai" and openai_tts:
                   colored_log(f"[Worker] Using OpenAI TTS direct playback for task {request.task_id}", "blue")
                   openai_tracking = {"active": True} # Simple tracking object
                   current_playback_info["openai_tracking"] = openai_tracking
                   playback_lock.release() # Release lock for potentially long OpenAI call
                   acquired_lock = False
                   success = False
                   try:
                       success = await openai_tts.stream_and_play(
                           text=request.text,
                           voice=request.params.get("voice"),
                           instructions=request.params.get("instructions"),
                           speed=request.params.get("speed", 1.0),
                           cancel_event=tts_cancel_requested # Pass the event
                       )
                   except asyncio.CancelledError:
                       colored_log(f"[Worker] OpenAI TTS task {request.task_id} directly cancelled.", "yellow")
                       raise # Re-raise immediately
                   finally: # Ensure lock is reacquired and tracking updated
                       if not acquired_lock: # If not already reacquired (e.g., due to exception)
                            await playback_lock.acquire()
                            acquired_lock = True
                       openai_tracking["active"] = False # Mark as inactive

                   # Check result *after* lock reacquired
                   if not success:
                       if tts_cancel_requested.is_set(): # Check if failure was due to cancellation
                           colored_log(f"[Worker] OpenAI playback likely failed due to cancellation for task {request.task_id}", "yellow")
                           raise asyncio.CancelledError("OpenAI playback cancelled by event")
                       else:
                           raise RuntimeError("OpenAI TTS playback failed or returned unsuccessful")
                   else:
                       colored_log(f"[Worker] OpenAI TTS playback completed for task {request.task_id}", "blue")

                else:
                    raise RuntimeError(f"Engine '{request.engine}' unavailable/invalid")

                # --- Success ---
                if task_future and not task_future.done():
                     task_future.set_result("Playback complete")

            except asyncio.CancelledError:
                 # This catches cancellations from the event logic above OR external cancellation
                 colored_log(f"[Worker] Task {request.task_id} cancelled during processing.", "yellow")
                 if task_future and not task_future.done():
                      # Ensure future reflects cancellation if not already set by stop tool
                      if not task_future.cancelled():
                           task_future.set_exception(asyncio.CancelledError("Playback cancelled"))
                 # Ensure process is terminated if worker is cancelled externally or by event
                 proc_cancel = current_playback_info.get("process") # Check info stored *before* cancellation handler
                 if proc_cancel and proc_cancel.returncode is None:
                     colored_log(f"[Worker Cancel Cleanup {request.task_id}] Terminating PID {proc_cancel.pid}", "grey")
                     try:
                         proc_cancel.terminate()
                         # Don't wait long here
                     except ProcessLookupError: pass
                     except Exception as term_err: colored_log(f"Error during cancel cleanup termination: {term_err}", "red")

            except Exception as e:
                err_msg = f"[Worker] Error processing task {request.task_id}: {type(e).__name__}: {e}"
                colored_log(err_msg, "red")
                traceback.print_exc(file=sys.stderr)
                if task_future and not task_future.done():
                    task_future.set_exception(e)

        except asyncio.CancelledError:
            colored_log("[Worker] Worker task itself cancelled.", "yellow")
            if acquired_lock: playback_lock.release() # Release lock if held
            if request and request.future and not request.future.done():
                 request.future.set_exception(asyncio.CancelledError("Worker cancelled"))
            # Ensure any lingering wait tasks are cancelled on worker exit
            if process_wait_task and not process_wait_task.done(): process_wait_task.cancel()
            if cancel_wait_task and not cancel_wait_task.done(): cancel_wait_task.cancel()
            break # Exit worker loop
        except Exception as e:
            colored_log(f"[Worker] Unexpected error in worker loop: {e}", "red")
            traceback.print_exc(file=sys.stderr)
            if acquired_lock: playback_lock.release()
            if request and request.future and not request.future.done():
                 request.future.set_exception(e)
            # Cleanup tasks just in case
            if process_wait_task and not process_wait_task.done(): process_wait_task.cancel()
            if cancel_wait_task and not cancel_wait_task.done(): cancel_wait_task.cancel()
            await asyncio.sleep(1) # Prevent rapid spinning on unexpected errors
        finally:
            if acquired_lock:
                # --- Worker Cleanup ---
                info_copy = current_playback_info # Use info potentially set before error/cancel
                proc_term = info_copy.get("process")
                pipe_cancel = info_copy.get("pipe_task")
                stderr_cancel = info_copy.get("stderr_task")
                task_id_cleaned = info_copy.get("task_id", "UNKNOWN")

                # Terminate process if still running (redundant for normal exit/event cancel, needed for errors)
                if proc_term and proc_term.returncode is None:
                     colored_log(f"[Worker Cleanup {task_id_cleaned}] Terminating PID {proc_term.pid}", "grey")
                     try:
                         proc_term.terminate()
                         await asyncio.wait_for(proc_term.wait(), timeout=0.2) # Short wait
                     except asyncio.TimeoutError: proc_term.kill()
                     except ProcessLookupError: pass
                     except Exception: pass

                # Cancel associated IO tasks
                if pipe_cancel and not pipe_cancel.done(): pipe_cancel.cancel()
                if stderr_cancel and not stderr_cancel.done(): stderr_cancel.cancel()

                # Cancel wait tasks if they are still around (e.g., unexpected error path)
                if process_wait_task and not process_wait_task.done(): process_wait_task.cancel()
                if cancel_wait_task and not cancel_wait_task.done(): cancel_wait_task.cancel()

                # Await cancellations briefly
                try:
                     tasks_to_await = filter(None, [pipe_cancel, stderr_cancel, process_wait_task, cancel_wait_task])
                     await asyncio.wait_for(asyncio.gather(*tasks_to_await, return_exceptions=True), timeout=0.2)
                except asyncio.TimeoutError: pass

                current_playback_info = {} # Clear playback info
                playback_lock.release()
                acquired_lock = False
                # --- End Worker Cleanup ---

            if request:
                 tts_queue.task_done()
                 colored_log(f"[Worker] Finished task {request.task_id} processing/cleanup.", "grey")

    colored_log("[Worker] Playback worker stopped.", "green")
# --- End Queue and Worker Setup ---


# --- Lifespan Management ---
@asynccontextmanager
async def lifespan_manager(server: FastMCP) -> AsyncGenerator[None, None]:
    """Server lifespan context manager."""
    global tts_queue, playback_worker_task, kokoro_tts_wrapper, openai_tts, current_playback_info
    colored_log("[Lifespan] Server starting up...", "yellow")
    tts_queue = asyncio.Queue()
    kokoro_tts_wrapper = KokoroTTSWrapper() # Initialize first
    openai_tts = None # Initialize as None
    if OPENAI_AVAILABLE:
         try: openai_tts = OpenAITTS()
         except Exception as e: colored_log(f"[Lifespan] Failed init OpenAI: {e}", "red")
    current_playback_info = {}
    tts_cancel_requested.clear() # Ensure event is clear on startup
    playback_worker_task = asyncio.create_task(playback_worker(), name="PlaybackWorker")

    try:
        yield # Server runs
    finally:
        colored_log("[Lifespan] Server shutting down...", "yellow")
        # 1. Signal worker AND any active playback via event
        tts_cancel_requested.set()
        if tts_queue: await tts_queue.put(None) # Signal worker queue

        # 2. Cancel queued items
        if tts_queue:
            while not tts_queue.empty():
                 try:
                     item = tts_queue.get_nowait()
                     if item is None: continue
                     if item.future and not item.future.done():
                          item.future.set_exception(asyncio.CancelledError("Shutdown"))
                     tts_queue.task_done()
                 except asyncio.QueueEmpty: break
                 except Exception as e: colored_log(f"[Lifespan] Error clearing queue item: {e}", "red")

        # 3. Attempt to gracefully stop current playback (Worker should handle via event)
        #    We still acquire lock briefly to potentially log the task being stopped.
        active_task_id_shutdown = None
        try:
            async with asyncio.timeout(1.0): # Timeout for lock acquisition
                async with playback_lock:
                     info_copy = current_playback_info.copy()
                     active_task_id_shutdown = info_copy.get("task_id")
                     # Don't need to manually kill process here, worker + event handles it
                     if active_task_id_shutdown:
                          colored_log(f"[Lifespan] Notified worker to cancel active task: {active_task_id_shutdown}", "yellow")
                     # Clear info immediately
                     current_playback_info = {}
                     # Cancel future if still present
                     future_shutdown = info_copy.get("future")
                     if future_shutdown and not future_shutdown.done():
                          future_shutdown.set_exception(asyncio.CancelledError("Shutdown"))
        except TimeoutError:
             colored_log("[Lifespan] Timeout acquiring lock during shutdown. Worker cancellation might be delayed.", "yellow")
        except Exception as e:
             colored_log(f"[Lifespan] Error during active task cleanup: {e}", "red")


        # 4. Wait for worker task to finish
        if playback_worker_task:
            try:
                await asyncio.wait_for(playback_worker_task, timeout=5.0)
                colored_log("[Lifespan] Worker finished cleanly.", "green")
            except asyncio.TimeoutError:
                colored_log("[Lifespan] Worker timed out, cancelling forcefully...", "yellow")
                playback_worker_task.cancel()
                try: await playback_worker_task
                except asyncio.CancelledError: pass # Expected
                except Exception as e: colored_log(f"[Lifespan] Error awaiting cancelled worker: {e}", "red")
            except Exception as e:
                 colored_log(f"[Lifespan] Error waiting for worker: {e}", "red")

        colored_log("[Lifespan] Shutdown complete.", "yellow")
# --- End Lifespan Management ---


# --- MCP Server Initialization ---
mcp = FastMCP(
    "tts_server_queued_fixed",
    lifespan=lifespan_manager,
    dependencies=["openai", "python-dotenv", "termcolor", "sounddevice", "numpy"] # Added numpy
    )

kokoro_tts_wrapper: Optional[KokoroTTSWrapper] = None
openai_tts: Optional[OpenAITTS] = None
# --- End MCP Server Initialization ---


# --- Tools ---
@mcp.tool()
async def tts(
    ctx: Context,
    text: str,
    engine: str = "kokoro",
    speed: float = 1.0,
    voice: str = "",
    instructions: str = ""
) -> str:
    """
    Convert text to speech using the preferred engine and streams the speach to the user.
    The base voice for the ai is the Kokoro engine, to keep ais personality consistent. 
    This unified function provides access to both Kokoro TTS (local) and OpenAI TTS (cloud API).
    Before calling this tool the first time its REQUIRED call the get_tts_instructions 
    tool.

    Args:
        text: Text to convert.
        engine: "kokoro" or "openai".
        speed: Speed (Kokoro: 0.8-1.5 typical, OpenAI: 0.8-1.5).
        voice: OpenAI voice name (ash, ballad, coral, sage, verse).
        instructions: Voice customization instructions for OpenAI TTS.

    Returns:
        Message indicating queue status and task ID.
    """
    if tts_queue is None: return "Error: TTS queue not ready."

    task_id = f"tts_{engine}_{int(time.time() * 1000)}_{os.urandom(4).hex()}"
    mcp_request_id = ctx.request_id
    colored_log(f"Received TTS request (MCP ID: {mcp_request_id}). Queuing task ID: {task_id}", "cyan")

    # Engine availability check
    if engine.lower() == "kokoro" and not (kokoro_tts_wrapper and kokoro_tts_wrapper.kokoro):
         return f"Error: Task {task_id} failed - Kokoro engine not available/initialized."
    if engine.lower() == "openai" and not openai_tts:
         return f"Error: Task {task_id} failed - OpenAI engine not available/initialized."
    if engine.lower() not in ["kokoro", "openai"]:
         return f"Error: Task {task_id} failed - Unknown engine '{engine}'."

    request_item = TTSRequest(
        task_id=task_id, mcp_request_id=mcp_request_id, text=text,
        engine=engine.lower(),
        params={"speed": speed, "voice": voice, "instructions": instructions},
        future=asyncio.Future() # Create future here
    )
    await tts_queue.put(request_item)
    q_size = tts_queue.qsize()
    return f"TTS request queued (Task ID: {task_id}). Position: {q_size}. Text: \"{text[:30]}...\""

@mcp.tool()
async def tts_stop_playback_and_clear_queue() -> str:
    """
    Stops the currently playing audio (if any) and clears all pending TTS requests from the queue.
    Relies on the background worker detecting the cancellation signal.
    """
    global current_playback_info, tts_queue, playback_lock, tts_cancel_requested
    if tts_queue is None: return "Error: TTS queue not available."

    colored_log("Received request to stop playback and clear queue.", "red")

    # 1. Signal cancellation universally via the event
    colored_log("Cancellation flag set - active TTS operations will check this.", "yellow")
    tts_cancel_requested.set()

    # 2. Attempt to kill external players (best effort for OpenAI fallback)
    player_killed = False
    try:
        cmd_map = {"darwin": "afplay", "linux": "aplay"}
        player_cmd = cmd_map.get(sys.platform)
        if player_cmd:
            # Use pkill with -f to match command name, check=False
            result = await asyncio.create_subprocess_exec("pkill", "-f", player_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await result.wait()
            if result.returncode == 0: player_killed = True
        if player_killed:
            colored_log(f"Attempted to kill running system audio player processes ({player_cmd}).", "yellow")
    except FileNotFoundError: pass # pkill might not be installed
    except Exception as e:
        colored_log(f"Error trying to kill audio players: {e}", "yellow")

    # 3. Briefly acquire lock ONLY to identify current task for logging
    cancelled_current_task_id: Optional[str] = None
    lock_acquired = False
    active_task_info = None
    try:
        async with asyncio.timeout(0.2): # Short timeout
             async with playback_lock:
                 lock_acquired = True
                 active_task_info = current_playback_info.copy()
                 # Don't interact with process/tasks here, worker handles it via event
                 # Cancel future directly if found
                 if active_task_info.get("task_id"):
                     cancelled_current_task_id = active_task_info.get("task_id")
                     future_stop = active_task_info.get("future")
                     if future_stop and not future_stop.done():
                          future_stop.set_exception(asyncio.CancelledError("Stopped by user tool"))
        if lock_acquired and cancelled_current_task_id:
             colored_log(f"Identified active task during cancellation: {cancelled_current_task_id}", "grey")
    except TimeoutError:
        colored_log("Couldn't acquire playback lock quickly - relying solely on cancellation event.", "yellow")
    except Exception as e:
        colored_log(f"Error during lock acquisition in stop tool: {e}", "red")

    # 4. Clear the queue (always do this)
    queue_cleared_count = 0
    cleared_items = []
    while tts_queue is not None and not tts_queue.empty():
         try:
             item = tts_queue.get_nowait()
             if item is None: continue
             cleared_items.append(item.task_id)
             if item.future and not item.future.done():
                  item.future.set_exception(asyncio.CancelledError("Queue cleared by user"))
             tts_queue.task_done()
             queue_cleared_count += 1
         except asyncio.QueueEmpty:
             break
         except Exception as e:
             colored_log(f"Error clearing queue item: {e}", "red")

    # 5. Construct final message based on outcome
    if cancelled_current_task_id:
        stopped_msg = f"Cancellation requested for active task: {cancelled_current_task_id}."
    elif lock_acquired:
        stopped_msg = "Cancellation requested. No active task ID was registered at the moment."
    else:
        stopped_msg = "Cancellation requested. Active task (if any) is being signalled to stop."

    cleared_msg = f"Cleared {queue_cleared_count} pending requests."
    if cleared_items:
        cleared_msg += f" (IDs: {', '.join(cleared_items[:3])}{'...' if len(cleared_items) > 3 else ''})"

    final_message = f"{stopped_msg} {cleared_msg}"
    colored_log(final_message, "green")
    return final_message



# New TTS examples tool with your crafted examples
@mcp.tool()
async def tts_examples(category: str = "general") -> str:
    """
    Provides research-based examples of effective voice instructions for OpenAI TTS.
    
    Args:
        category: Type of examples to retrieve ("general", "accents", "characters", "emotions", "narration")
        
    Returns:
        Example instructions with descriptions
    """
    examples = {
        "general": [
            {
                "description": "Clear, friendly virtual assistant",
                "instructions": "Speak in a clear, friendly tone as if greeting someone cheerfully.",
                "voice": "coral",
                "best_for": "Chatbot introductions, AI receptionists, welcome messages"
            },
            {
                "description": "Professional presenter",
                "instructions": "Speak in a clear, professional tone with moderate pacing and precise articulation.",
                "voice": "ash",
                "best_for": "Business presentations, product demonstrations, formal announcements"
            },
            {
                "description": "Motivational speaker",
                "instructions": "Deliver your message with an inspiring and energetic tone that builds excitement and drives action.",
                "voice": "verse",
                "best_for": "Motivational talks, educational content, workout guidance"
            },
            {
                "description": "Casual explainer",
                "instructions": "Speak in a relaxed yet clear manner, simplifying complex ideas with a natural, conversational flow.",
                "voice": "coral",
                "best_for": "Educational tutorials, explainer videos, casual briefings"
            },
            {
                "description": "Dynamic storyteller",
                "instructions": "Use a lively, engaging tone that dynamically shifts with the content, emphasizing key points.",
                "voice": "ash",
                "best_for": "Podcasts, interactive narratives, dynamic content delivery"
            },
            {
                "description": "Engaging announcer",
                "instructions": "Speak with a confident and engaging tone, capturing attention while maintaining clarity and warmth.",
                "voice": "verse",
                "best_for": "Event announcements, radio ads, public service messages"
            }
        ],
        "accents": [
            {
                "description": "British speaker",
                "instructions": "Adopt a polite British accent with refined enunciation and a measured, explanatory tone.",
                "voice": "ballad",
                "best_for": "Educational narrations, museum guides, upscale product tutorials"
            },
            {
                "description": "French accent",
                "instructions": "Use a smooth French accent with elongated vowels and a melodic, flowing rhythm.",
                "voice": "coral",
                "best_for": "Cultural content, character voices, international narration"
            },
            {
                "description": "American accent",
                "instructions": "Speak in a clear, neutral American accent with friendly, approachable inflections.",
                "voice": "ash",
                "best_for": "News reports, corporate videos, e-learning content"
            },
            {
                "description": "Irish lilt",
                "instructions": "Incorporate a gentle Irish lilt with warm, musical intonations that evoke a sense of storytelling.",
                "voice": "verse",
                "best_for": "Narrative content, cultural storytelling, informal guides"
            },
            {
                "description": "Scottish brogue",
                "instructions": "Adopt a rich Scottish brogue with robust, crisp pronunciation and a hint of rugged charm.",
                "voice": "ballad",
                "best_for": "Historical documentaries, character voices, immersive narratives"
            },
            {
                "description": "Australian twang",
                "instructions": "Speak with a relaxed Australian accent that blends friendliness with a laid-back rhythm.",
                "voice": "coral",
                "best_for": "Travel guides, informal discussions, casual narration"
            }
        ],
        "characters": [
            {
                "description": "Pirate character",
                "instructions": "Talk like a gruff old pirate with hearty laughs and a characteristic 'Arrr!' to punctuate your speech.",
                "voice": "ash",
                "best_for": "Games, entertainment apps, interactive children's content"
            },
            {
                "description": "Wise mentor",
                "instructions": "Adopt a measured, thoughtful tone as if imparting decades of wisdom, with gentle pauses for emphasis.",
                "voice": "sage",
                "best_for": "Educational content, advice sections, guidance materials"
            },
            {
                "description": "Energetic superhero",
                "instructions": "Speak with bold confidence and a powerful, determined tone that conveys strength and optimism.",
                "voice": "ash",
                "best_for": "Comic book narrations, action scenes, heroic dialogues"
            },
            {
                "description": "Quirky sidekick",
                "instructions": "Adopt a playful, spontaneous tone with light humor and a friendly cadence that enhances character charm.",
                "voice": "coral",
                "best_for": "Animated series, comedy sketches, light-hearted narratives"
            },
            {
                "description": "Mysterious detective",
                "instructions": "Use a slightly hushed, contemplative tone with deliberate pauses, evoking a sense of mystery and intrigue.",
                "voice": "sage",
                "best_for": "Mystery stories, detective narratives, suspenseful audio dramas"
            },
            {
                "description": "Sassy sidekick",
                "instructions": "Speak with a cheeky, confident tone laced with humor and a hint of sarcasm, yet remaining approachable.",
                "voice": "coral",
                "best_for": "Comedic dialogues, animated features, interactive storytelling"
            },
            {
                "description": "Villainous mastermind",
                "instructions": "Deliver lines with a smooth yet menacing tone, using controlled pace and subtle inflections to imply dark intent.",
                "voice": "sage",
                "best_for": "Dramatic narratives, villain characters in games or films, suspenseful content"
            }
        ],
        "emotions": [
            {
                "description": "Empathetic counselor",
                "instructions": "Speak in a calm, empathetic tone with slow, gentle pacing, as if offering sincere comfort.",
                "voice": "sage",
                "best_for": "Mental health apps, sensitive customer service, supportive content"
            },
            {
                "description": "Enthusiastic announcer",
                "instructions": "Use energetic, vibrant delivery with an upbeat tempo to create an atmosphere of celebration.",
                "voice": "ash",
                "best_for": "Announcements, congratulatory messages, celebratory events"
            },
            {
                "description": "Calm reassurance",
                "instructions": "Adopt a soothing, measured tone with soft inflections to instill a sense of calm and security.",
                "voice": "ballad",
                "best_for": "Meditation guides, sleep stories, relaxation apps"
            },
            {
                "description": "Excited cheerleader",
                "instructions": "Speak with buoyant energy and infectious enthusiasm, emphasizing key moments with bursts of excitement.",
                "voice": "verse",
                "best_for": "Sports events, uplifting messages, energetic updates"
            },
            {
                "description": "Melancholic reflection",
                "instructions": "Deliver your lines in a soft, introspective tone with gentle, wistful pauses that capture bittersweet emotion.",
                "voice": "ballad",
                "best_for": "Poetic narrations, reflective commentary, emotional storytelling"
            },
            {
                "description": "Joyful exuberance",
                "instructions": "Speak with bright, lively tones and an upbeat rhythm, as if sharing exciting and happy news.",
                "voice": "coral",
                "best_for": "Celebratory messages, festive announcements, joyful storytelling"
            },
            {
                "description": "Serene optimism",
                "instructions": "Adopt a calm, confident tone with a smooth, steady cadence that conveys hope and reassurance.",
                "voice": "sage",
                "best_for": "Motivational content, inspirational narratives, wellness apps"
            }
        ],
        "narration": [
            {
                "description": "Bedtime storyteller",
                "instructions": "Narrate with a soft, soothing tone that gently guides the listener into a relaxed, sleep-ready state.",
                "voice": "verse",
                "best_for": "Bedtime stories, relaxation apps, gentle audiobooks"
            },
            {
                "description": "Documentary narrator",
                "instructions": "Speak clearly and authoritatively, with measured emphasis and subtle tones of wonder to enhance factual storytelling.",
                "voice": "verse",
                "best_for": "Documentaries, educational videos, informative content"
            },
            {
                "description": "Epic saga narrator",
                "instructions": "Use a dramatic, resonant tone with deliberate pauses to build suspense and convey the grandeur of the narrative.",
                "voice": "ash",
                "best_for": "Epic audiobooks, fantasy narrations, long-form storytelling"
            },
            {
                "description": "Children's story narrator",
                "instructions": "Adopt a playful, animated tone with clear enunciation and varying inflections to keep young listeners engaged.",
                "voice": "coral",
                "best_for": "Children's audiobooks, interactive story apps, fairy tales"
            },
            {
                "description": "Nature documentary narrator",
                "instructions": "Speak in a calm, authoritative tone interspersed with moments of quiet wonder, evoking vivid imagery of the natural world.",
                "voice": "sage",
                "best_for": "Nature documentaries, educational travel guides, environmental content"
            },
            {
                "description": "Historical documentary narrator",
                "instructions": "Use a measured, respectful tone with occasional formal inflections to underscore the gravity and detail of historical events.",
                "voice": "ballad",
                "best_for": "History documentaries, archival narrations, cultural retrospectives"
            },
            {
                "description": "Science fiction narrative",
                "instructions": "Speak with a cool, futuristic tone combined with precise diction to evoke a sense of otherworldliness and discovery.",
                "voice": "verse",
                "best_for": "Sci-fi audiobooks, futuristic podcasts, imaginative storytelling"
            }
        ]
    }
    
    if category not in examples:
        valid_categories = ", ".join(examples.keys())
        return f"Invalid category. Please choose from: {valid_categories}"
    
    selected_examples = examples[category]
    result = f"# {category.capitalize()} Voice Instruction Examples\n\n"
    result += "Note: These instructions are designed to work exclusively with OpenAI voices, not with Kokoro.\n\n"
    
    for ex in selected_examples:
        result += f"## {ex['description']}\n"
        result += f"**Best for:** {ex['best_for']}\n"
        result += f"**Recommended voice:** {ex['voice']}\n"
        result += f"**Instructions:** \"{ex['instructions']}\"\n\n"
        result += "Example usage:\n"
        result += f"```\ntts(\"Your text here\", engine=\"openai\", voice=\"{ex['voice']}\", instructions=\"{ex['instructions']}\")\n```\n\n"
    
    return result

# --- End Tools ---

@mcp.tool()
def get_tts_instructions() -> str:
    """Fetches TTS instructions by calling get_voice_info."""
    return get_voice_info()
# Updated resource for voice informat
# ion
@mcp.resource("tts://voices")
def get_voice_info() -> str:
    """Information about available TTS voices (recommended to read before first use)."""
    return """# TTS Voices and Usage Guidelines (Read Before First Use)

## Function Parameters
- text: Text to convert to speech
- engine: TTS engine to use ("kokoro" or "openai", default: "kokoro") 
- speed: Speech speed between 0.75-2.0 (applies to Kokoro only)
- voice: Voice to use (applies to OpenAI only)
- instructions: Instructions to customize the OpenAI voice (applies to OpenAI only)

## Selection Guidelines
- Use Kokoro (default) unless:
  - The user explicitly requests OpenAI TTS
  - Voice customization is needed
  - The content would benefit from more natural-sounding speech (but do not change without asking the user)
- Speed parameter (0.75-2.0) only affects Kokoro
- Voice and instructions parameters only affect OpenAI
- Keep voices consistent over time to avoid altering the personality of the voice
- Leave voice and instructions empty when using Kokoro

## Kokoro TTS (Local)
- Default engine for responses
- Supports speed adjustment (0.75-2.0)
- Fixed voice (af_heart)
- Use for: Simple announcements, notifications, and any final answer as a direct answer for the user.
- Is the AIs personal voice to keep personality of the agent consistent, and recognicable by the user.
- Can be used as direct responses of inside messages eiven when combined with openai tts.

## OpenAI TTS (Cloud) - GPT-4o-mini-tts model
- Natural-sounding with voice customization through instructions parameter
- Supports "voice steering" via text instructions

### Recommended Voices:

#### Ash
- Enthusiastic, energetic, and lively voice with a bright, animated tone
- Young male speaker, engaging and upbeat with emotive, dynamic delivery
- Best for: Upbeat presentations, advertisements, interactive dialogues, game characters

#### Ballad
- Romantic, emotional voice with a gentle rhythm and British-sounding accent
- Young male voice with refined, narrative quality; smooth and heartfelt tone
- Best for: Storytelling, audiobooks, historical narratives, poetic content

#### Coral
- Cheerful, friendly, and community-oriented voice
- Young female speaker with warm, inviting, upbeat yet conversational tone
- Best for: Customer support, virtual assistants, educational content, welcoming scenarios

#### Sage
- Wise, calm, and knowledgeable voice
- Young female voice with patient authority; steady and composed cadence
- Best for: Informational content, meditation apps, instructional podcasts, training materials

#### Verse
- Versatile, expressive narrator voice with balanced tone and slight dramatic flair
- Adult male voice that's articulate and poetic, emphasizing context-appropriate words
- Best for: General narration, documentaries, audiobooks, creative storytelling, video essays

Note: Instructions parameter works only with OpenAI voices, not with Kokoro.
Voice examples can be found using the tts_examples() tool.
"""

# --- End Resource ---


# --- Main Execution Block ---
if __name__ == "__main__":
    colored_log("Starting Queued Streaming Async TTS MCP server (v3.1 Kokoro Fix)...", "green")

    # Perform checks *before* initializing engines in lifespan
    kokoro_present = KokoroTTS is not None
    openai_present = OPENAI_AVAILABLE
    openai_streaming_ok = AUDIO_STREAMING_AVAILABLE if 'AUDIO_STREAMING_AVAILABLE' in globals() else False

    if kokoro_present: colored_log("✓ Kokoro TTS code present", "green")
    else: colored_log("✗ Kokoro TTS code not found", "red")

    if openai_present:
        colored_log("✓ OpenAI configured", "green")
        if openai_streaming_ok:
            colored_log("  ✓ True streaming available (via LocalAudioPlayer with cancellation)", "green")
        else:
            colored_log("  ✗ True streaming/cancellation support might be limited. Install/update 'openai[voice_helpers]>=1.23.0'", "yellow")
    else:
        colored_log("✗ OpenAI unavailable (No API key or library missing)", "red")

    colored_log("✓ Active Tools:", "green")
    colored_log("  - tts(...)", "cyan")
    colored_log("  - tts_stop_playback_and_clear_queue()", "cyan")
    colored_log("  - tts_examples(...)", "cyan")
    colored_log("✓ Resources:", "green")
    colored_log("  - tts://voices", "cyan")

    try:
        colored_log("Starting MCP server...", "green")
        mcp.run()
    except KeyboardInterrupt:
        try:
            colored_log("\nServer stopped by user (KeyboardInterrupt)", "yellow")
        except (ValueError, IOError): pass # Ignore IO errors during shutdown logging
    except Exception as e:
        try:
            colored_log(f"CRITICAL Error running server: {type(e).__name__}: {str(e)}", "red")
            traceback.print_exc(file=sys.stderr)
        except (ValueError, IOError): pass
        sys.exit(1)
    finally:
         # Final log to indicate clean exit if possible
         try:
              colored_log("MCP server process finished.", "blue")
         except Exception: pass
# --- End Main Execution Block ---