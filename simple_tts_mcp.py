#!/usr/bin/env python3
"""
Simplified MCP Server for Kokoro TTS (Async Queued Streaming Playback)

- Streamlined version focused only on Kokoro TTS
- Maintains the core streaming functionality with simplified interface
- TTS requests are added to a queue
- A background worker processes the queue sequentially
- Audio playback starts streaming using ffplay
- Provides a simple cancellation mechanism via the main tts function
"""
import sys
import os
import asyncio
import time
import subprocess
import traceback
from typing import Optional, Dict, Any, AsyncGenerator, Tuple
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from io import BytesIO
import numpy as np

# Print to stderr function
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

# Optional termcolor for prettier logging
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

# --- Playback Function ---
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


# --- Kokoro TTS Class ---
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
                    yield (audio * 32767).astype(np.int16).tobytes()
                colored_log(f"[Kokoro] Finished streaming {segment_count} segments (pipeline).", "blue")

            # Fallback to older segment generation method if pipeline not available/suitable
            elif hasattr(self.kokoro, '_generate_segments'):
                colored_log("[Kokoro] Using _generate_segments fallback.", "yellow")
                for audio, sr in self.kokoro._generate_segments(text, voice="af_heart", speed=speed):
                     if len(audio) == 0: continue
                     if sr != sample_rate: colored_log(f"[Kokoro] Warning: Sample rate mismatch {sr} != {sample_rate}", "yellow")
                     segment_count += 1
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
# --- End Kokoro TTS Class ---


# --- Queue and Worker Setup ---
@dataclass
class TTSRequest:
    task_id: str
    mcp_request_id: Any
    text: str
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
        process_wait_task: Optional[asyncio.Task] = None
        cancel_wait_task: Optional[asyncio.Task] = None

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
                "task_id": request.task_id,
                "process": None, "pipe_task": None, "stderr_task": None,
                "future": task_future
            }

            try:
                # --- Process with Kokoro engine ---
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
                                 if process_wait_task in done or was_cancelled:
                                     return_code = await process_wait_task
                            except asyncio.CancelledError:
                                 if not was_cancelled:
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

                # --- Success ---
                if task_future and not task_future.done():
                     task_future.set_result("Playback complete")

            except asyncio.CancelledError:
                 colored_log(f"[Worker] Task {request.task_id} cancelled during processing.", "yellow")
                 if task_future and not task_future.done():
                      if not task_future.cancelled():
                           task_future.set_exception(asyncio.CancelledError("Playback cancelled"))
                 # Ensure process is terminated if worker is cancelled externally or by event
                 proc_cancel = current_playback_info.get("process") # Check info stored *before* cancellation handler
                 if proc_cancel and proc_cancel.returncode is None:
                     colored_log(f"[Worker Cancel Cleanup {request.task_id}] Terminating PID {proc_cancel.pid}", "grey")
                     try:
                         proc_cancel.terminate()
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

                # Terminate process if still running
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

                # Cancel wait tasks if they are still around
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

            if request:
                 tts_queue.task_done()
                 colored_log(f"[Worker] Finished task {request.task_id} processing/cleanup.", "grey")

    colored_log("[Worker] Playback worker stopped.", "green")
# --- End Queue and Worker Setup ---


# --- Lifespan Management ---
@asynccontextmanager
async def lifespan_manager(server: FastMCP) -> AsyncGenerator[None, None]:
    """Server lifespan context manager."""
    global tts_queue, playback_worker_task, kokoro_tts_wrapper, current_playback_info
    colored_log("[Lifespan] Server starting up...", "yellow")
    tts_queue = asyncio.Queue()
    kokoro_tts_wrapper = KokoroTTSWrapper() # Initialize
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

        # 3. Attempt to gracefully stop current playback
        active_task_id_shutdown = None
        try:
            async with asyncio.timeout(1.0): # Timeout for lock acquisition
                async with playback_lock:
                     info_copy = current_playback_info.copy()
                     active_task_id_shutdown = info_copy.get("task_id")
                     if active_task_id_shutdown:
                          colored_log(f"[Lifespan] Notified worker to cancel active task: {active_task_id_shutdown}", "yellow")
                     current_playback_info = {}
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
    "simple_kokoro_tts_server",
    lifespan=lifespan_manager,
    dependencies=["termcolor", "sounddevice", "numpy"]
    )

kokoro_tts_wrapper: Optional[KokoroTTSWrapper] = None
# --- End MCP Server Initialization ---


# --- Tool ---
@mcp.tool()
async def tts(
    ctx: Context,
    text: str,
    speed: float = 1.0,
    clear_queue: bool = False
) -> str:
    """
    Convert text to speech using Kokoro TTS and stream the speech to the user.
    This is the base voice for the AI when needing to speak.

    Args:
        text: Text to convert to speech.
        speed: Speech speed (0.8-1.5 typical).
        clear_queue: If True, clears any existing TTS queue before adding this request.

    Returns:
        Message indicating queue status and task ID.
    """
    if tts_queue is None: return "Error: TTS queue not ready."

    # Handle clear_queue option
    if clear_queue:
        colored_log("Clearing TTS queue as requested.", "yellow")
        tts_cancel_requested.set()  # Signal cancellation of current playback
        
        # Clear existing items in queue
        cleared_count = 0
        while not tts_queue.empty():
            try:
                item = tts_queue.get_nowait()
                if item is None: continue
                if item.future and not item.future.done():
                    item.future.set_exception(asyncio.CancelledError("Queue cleared by user"))
                tts_queue.task_done()
                cleared_count += 1
            except asyncio.QueueEmpty:
                break
            except Exception as e:
                colored_log(f"Error clearing queue item: {e}", "red")
        
        if cleared_count > 0:
            colored_log(f"Cleared {cleared_count} pending requests from queue", "yellow")

    # Create and queue the new TTS request
    task_id = f"tts_kokoro_{int(time.time() * 1000)}_{os.urandom(4).hex()}"
    mcp_request_id = ctx.request_id
    colored_log(f"Received TTS request (MCP ID: {mcp_request_id}). Queuing task ID: {task_id}", "cyan")

    # Engine availability check
    if not (kokoro_tts_wrapper and kokoro_tts_wrapper.kokoro):
        return f"Error: Task {task_id} failed - Kokoro engine not available/initialized."

    request_item = TTSRequest(
        task_id=task_id, mcp_request_id=mcp_request_id, text=text,
        params={"speed": speed},
        future=asyncio.Future() # Create future here
    )
    await tts_queue.put(request_item)
    q_size = tts_queue.qsize()
    
    # Adjust return message based on if queue was cleared
    if clear_queue:
        return f"Queue cleared. New TTS request queued (Task ID: {task_id}). Text: \"{text[:30]}...\""
    else:
        return f"TTS request queued (Task ID: {task_id}). Position: {q_size}. Text: \"{text[:30]}...\""
# --- End Tool ---


# --- Main Execution Block ---
if __name__ == "__main__":
    colored_log("Starting Simple Kokoro TTS MCP server...", "green")

    # Check if Kokoro is available
    if KokoroTTS is not None:
        colored_log("✓ Kokoro TTS code present", "green")
    else:
        colored_log("✗ Kokoro TTS code not found", "red")

    colored_log("✓ Active Tool:", "green")
    colored_log("  - tts(text, speed=1.0, clear_queue=False)", "cyan")

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