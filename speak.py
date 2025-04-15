#!/usr/bin/env python3
"""
Kokoro TTS CLI - A simple command-line tool for text-to-speech conversion.

Usage:
    uv run speak.py "Text to be spoken" [--voice VOICE] [--speed SPEED] [--no-play] [--format FORMAT]

Options:
    --voice VOICE     Voice to use (default: af_heart)
    --speed SPEED     Speech speed (default: 1.0)
    --no-play         Generate audio file without playing it
    --format FORMAT   Output format: wav or mp3 (default: wav)
    --output FILE     Save to specific output file instead of temporary file
    --no-stream       Don't stream audio as it's generated (wait until all is ready)
"""
import sys
import os
import time
import tempfile
import subprocess
import argparse
import asyncio
import queue
import threading
from termcolor import colored
from pathlib import Path
from io import BytesIO
import numpy as np
import soundfile as sf
from kokoro import KPipeline

try:
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline
except ImportError:
    print(colored("Installing required packages...", "yellow"))
    subprocess.call([sys.executable, "-m", "pip", "install", "numpy", "soundfile", "termcolor", "kokoro"])
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline

class KokoroTTS:
    def __init__(self):
        """Initialize Kokoro TTS pipeline."""
        print(colored("Initializing Kokoro TTS pipeline...", "cyan"))
        self.pipeline = KPipeline(lang_code='a')  # American English
        print(colored("Kokoro TTS pipeline ready!", "green"))
        
    def _generate_segments(self, text: str, voice='af_heart', speed=1.0):
        """Internal method to generate speech segments.
        
        Args:
            text: Text to convert to speech
            voice: Voice to use
            speed: Speech speed
            
        Yields:
            Tuple of (audio_data, sample_rate)
        """
        if not text.strip():
            raise ValueError("Empty text provided")
            
        print(colored(f"Generating speech for text: {text[:100]}{'...' if len(text) > 100 else ''}", "cyan"))
        start_time = time.time()
        
        # Generate audio segments
        generator = self.pipeline(
            text,
            voice=voice,
            speed=speed,
            split_pattern=r'[.!?]\s+'  # Split on sentence boundaries
        )
        
        segment_count = 0
        for i, (graphemes, phonemes, audio) in enumerate(generator):
            if len(audio) == 0:
                print(colored(f"Warning: Empty audio in segment {i+1}", "yellow"))
                continue
                
            # Convert to float32 numpy array if needed
            if not isinstance(audio, np.ndarray):
                audio = np.array(audio, dtype=np.float32)
                
            segment_count += 1
            print(colored(f"Generated segment {segment_count}", "green"))
            yield audio, 24000  # Kokoro uses 24kHz sample rate
            
        elapsed = time.time() - start_time
        print(colored(f"Speech generation completed in {elapsed:.2f}s", "green"))
        
    async def generate_speech(self, text: str, voice='af_heart', speed=1.0):
        """Generate speech from text using Kokoro.
        
        Args:
            text: Text to convert to speech
            voice: Voice to use (default: af_heart)
            speed: Speech speed (default: 1.0)
            
        Returns:
            List of tuples (audio_data, sample_rate) for each generated segment
        """
        try:
            return list(self._generate_segments(text, voice, speed))
        except Exception as e:
            print(colored(f"Error generating speech: {str(e)}", "red"))
            raise

    async def stream_and_save(self, text: str, voice='af_heart', speed=1.0, output_file=None, 
                          format='wav', play=True, no_stream=False):
        """Stream speech as it's generated while also saving to a file.
        
        Args:
            text: Text to convert to speech
            voice: Voice to use (default: af_heart)
            speed: Speech speed (default: 1.0)
            output_file: Optional path to save the audio file
            format: Output format ('wav' or 'mp3')
            play: Whether to play the audio while generating
            no_stream: Don't stream audio as it's generated (wait until all is ready)
            
        Returns:
            Path to the generated audio file
        """
        # Create temporary files for streaming
        temp_dir = tempfile.mkdtemp()
        combined_segments = []
        segment_files = []
        
        try:
            # Set up for streaming playback
            audio_queue = queue.Queue()
            playback_thread = None
            
            if play and not no_stream:
                # Start playback thread that will play segments as they come in
                playback_thread = threading.Thread(
                    target=self._stream_playback_worker,
                    args=(audio_queue,),
                    daemon=True
                )
                playback_thread.start()
            
            # Generate and process each segment
            for i, (audio, sample_rate) in enumerate(self._generate_segments(text, voice, speed)):
                combined_segments.append(audio)
                
                if play and not no_stream:
                    # Save segment to temp file
                    segment_path = os.path.join(temp_dir, f"segment_{i}.wav")
                    sf.write(
                        segment_path,
                        audio,
                        sample_rate,
                        format='WAV',
                        subtype='PCM_16'
                    )
                    segment_files.append(segment_path)
                    
                    # Queue for playback
                    audio_queue.put(segment_path)
            
            # Signal that no more segments are coming
            if play and not no_stream:
                audio_queue.put(None)
            
            # Combine all segments into the final output file
            if not combined_segments:
                raise ValueError("No audio segments were generated")
            
            combined_audio = np.concatenate(combined_segments)
            
            # Determine final output file path
            final_path = None
            
            if format == 'mp3':
                # For MP3, first save as WAV then convert
                temp_wav = os.path.join(temp_dir, "combined.wav")
                sf.write(
                    temp_wav,
                    combined_audio,
                    24000,
                    format='WAV',
                    subtype='PCM_16'
                )
                
                # Determine MP3 path
                if output_file:
                    mp3_path = output_file
                else:
                    mp3_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
                    mp3_path = mp3_file.name
                    mp3_file.close()
                
                # Convert to MP3
                cmd = [
                    "ffmpeg", 
                    "-i", temp_wav, 
                    "-codec:a", "libmp3lame", 
                    "-qscale:a", "2", 
                    "-y",  # Overwrite existing file
                    mp3_path
                ]
                
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                stdout, stderr = await process.communicate()
                
                if process.returncode != 0:
                    print(colored(f"FFmpeg error: {stderr.decode()}", "red"))
                    final_path = temp_wav  # Use WAV as fallback
                else:
                    print(colored(f"MP3 file saved: {mp3_path}", "green"))
                    final_path = mp3_path
            else:
                # Save as WAV
                if output_file:
                    wav_path = output_file
                else:
                    wav_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
                    wav_path = wav_file.name
                    wav_file.close()
                
                sf.write(
                    wav_path,
                    combined_audio,
                    24000,
                    format='WAV',
                    subtype='PCM_16'
                )
                print(colored(f"WAV file saved: {wav_path} ({len(combined_segments)} segments)", "green"))
                final_path = wav_path
            
            # Wait for streaming playback to complete if it was started
            if playback_thread and not no_stream:
                playback_thread.join()
            
            # If not streaming or if no_stream is set, play the combined file
            if play and (no_stream or not playback_thread):
                play_audio(final_path)
            
            return final_path
            
        except Exception as e:
            print(colored(f"Error in stream_and_save: {str(e)}", "red"))
            raise
        finally:
            # Clean up temp directory and segment files
            for segment_file in segment_files:
                try:
                    if os.path.exists(segment_file):
                        os.unlink(segment_file)
                except Exception:
                    pass
            
            try:
                # Remove temp directory
                if os.path.exists(temp_dir):
                    os.rmdir(temp_dir)
            except Exception:
                pass
    
    def _stream_playback_worker(self, audio_queue):
        """Worker thread that plays audio segments as they become available in the queue.
        
        Args:
            audio_queue: Queue containing paths to audio segment files
        """
        print(colored("Starting audio streaming...", "cyan"))
        segment_num = 0
        
        while True:
            segment_path = audio_queue.get()
            
            # None is the signal that no more segments are coming
            if segment_path is None:
                break
                
            segment_num += 1
            print(colored(f"Playing segment {segment_num}...", "cyan"))
            
            # Play the segment
            play_audio(segment_path, verbose=False)
            
            # Mark task as done
            audio_queue.task_done()
        
        print(colored("Audio streaming completed", "green"))

    async def text_to_wav(self, text: str, voice='af_heart', speed=1.0, output_file=None):
        """Convert text to a single WAV file.
        
        Args:
            text: Text to convert to speech
            voice: Voice to use (default: af_heart)
            speed: Speech speed (default: 1.0)
            output_file: Optional path to save the WAV file
            
        Returns:
            Path to generated WAV file
        """
        try:
            # Collect all segments first
            all_segments = []
            for audio, sample_rate in self._generate_segments(text, voice, speed):
                all_segments.append(audio)
            
            if not all_segments:
                raise ValueError("No audio segments were generated")
            
            # Combine all segments into one audio array
            combined_audio = np.concatenate(all_segments)
            
            # Create file for output
            if output_file:
                file_path = output_file
                temp_file = False
            else:
                # Create temporary WAV file
                temp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
                file_path = temp.name
                temp_file = True
                temp.close()
            
            # Write WAV file with proper headers
            sf.write(
                file_path,
                combined_audio,
                24000,
                format='WAV',
                subtype='PCM_16'
            )
            print(colored(f"WAV file saved: {file_path} ({len(all_segments)} segments)", "green"))
            return file_path
                
        except Exception as e:
            print(colored(f"Error saving WAV file: {str(e)}", "red"))
            raise

    async def text_to_mp3(self, text: str, voice='af_heart', speed=1.0, output_file=None):
        """Convert text to a single MP3 file.
        
        Args:
            text: Text to convert to speech
            voice: Voice to use (default: af_heart)
            speed: Speech speed (default: 1.0)
            output_file: Optional path to save the MP3 file
            
        Returns:
            Path to generated MP3 file
        """
        try:
            # First generate WAV file
            wav_file = await self.text_to_wav(text, voice, speed)
            
            # Create output MP3 file path
            if output_file:
                mp3_file = output_file
            else:
                mp3_file = wav_file.replace('.wav', '.mp3')
            
            # Convert WAV to MP3 using ffmpeg
            cmd = [
                "ffmpeg", 
                "-i", wav_file, 
                "-codec:a", "libmp3lame", 
                "-qscale:a", "2", 
                "-y",  # Overwrite existing file
                mp3_file
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                print(colored(f"FFmpeg error: {stderr.decode()}", "red"))
                return wav_file  # Return WAV file as fallback
                
            print(colored(f"MP3 file saved: {mp3_file}", "green"))
            
            # Clean up temporary WAV file
            if output_file is None:
                try:
                    os.unlink(wav_file)
                except Exception as e:
                    print(colored(f"Warning: Could not clean up WAV file ({str(e)})", "yellow"))
            
            return mp3_file
                
        except Exception as e:
            print(colored(f"Error saving MP3 file: {str(e)}", "red"))
            raise

    async def stream_speech(self, text: str, voice='af_heart', speed=1.0):
        """Stream speech segments as they are generated.
        
        Args:
            text: Text to convert to speech
            voice: Voice to use (default: af_heart)
            speed: Speed of speech (default: 1.0)
            
        Yields:
            Bytes of WAV-encoded audio in streamable format
        """
        try:
            # First pass: calculate total audio length
            total_samples = 0
            segments = []
            
            for audio, sample_rate in self._generate_segments(text, voice, speed):
                segments.append(audio)
                total_samples += len(audio)
            
            # Calculate total size for WAV header
            total_bytes = total_samples * 2  # 16-bit audio = 2 bytes per sample
            header_size = 44  # Standard WAV header size
            total_size = header_size + total_bytes
            
            # Write WAV header
            header = BytesIO()
            # RIFF header
            header.write(b'RIFF')
            header.write(total_size.to_bytes(4, 'little'))  # Total file size
            header.write(b'WAVE')
            # fmt chunk
            header.write(b'fmt ')
            header.write((16).to_bytes(4, 'little'))  # Chunk size
            header.write((1).to_bytes(2, 'little'))   # PCM format
            header.write((1).to_bytes(2, 'little'))   # Mono
            header.write((24000).to_bytes(4, 'little'))  # Sample rate
            header.write((48000).to_bytes(4, 'little'))  # Byte rate (24000 * 2)
            header.write((2).to_bytes(2, 'little'))   # Block align
            header.write((16).to_bytes(2, 'little'))  # Bits per sample
            # data chunk
            header.write(b'data')
            header.write(total_bytes.to_bytes(4, 'little'))
            
            # Send header
            header.seek(0)
            yield header.read()
            
            # Stream audio data
            for audio in segments:
                yield (audio * 32767).astype(np.int16).tobytes()
                    
        except Exception as e:
            print(colored(f"Error in stream_speech: {str(e)}", "red"))
            raise

def play_audio(file_path, verbose=True):
    """
    Play audio using system player.
    
    Args:
        file_path: Path to the audio file
        verbose: Whether to print status messages
    """
    if not file_path or not os.path.exists(file_path):
        if verbose:
            print(colored("No audio file to play", "red"))
        return
        
    if verbose:
        print(colored(f"Playing audio from: {file_path}", "cyan"))
    
    # Play the audio using the system's audio player
    if sys.platform == "darwin":  # macOS
        subprocess.run(["afplay", file_path], check=True)
    elif sys.platform == "linux":
        subprocess.run(["aplay", file_path], check=True)
    elif sys.platform == "win32":
        subprocess.run(["start", file_path], shell=True, check=True)
    else:
        if verbose:
            print(colored(f"Unsupported platform: {sys.platform}. Audio saved to {file_path}", "yellow"))
        return
    
    if verbose:
        print(colored("Playback complete!", "green"))

async def async_main(args):
    """Async main function"""
    try:
        tts = KokoroTTS()
        
        # Use the new streaming method
        output_file = await tts.stream_and_save(
            args.text,
            voice=args.voice,
            speed=args.speed,
            output_file=args.output,
            format=args.format,
            play=not args.no_play,
            no_stream=args.no_stream
        )
        
        # Clean up the temporary file unless user specified output file
        if not args.output and not args.no_play:
            try:
                os.unlink(output_file)
                print(colored("Temporary file cleaned up", "green"))
            except Exception as e:
                print(colored(f"Warning: Could not clean up file ({str(e)})", "yellow"))
    
    except Exception as e:
        print(colored(f"Error: {str(e)}", "red"))
        return 1
    
    return 0

def main():
    """Main entry point for the CLI"""
    parser = argparse.ArgumentParser(description="Convert text to speech using Kokoro TTS")
    parser.add_argument("text", nargs="?", help="Text to convert to speech")
    parser.add_argument("--voice", default="af_heart", help="Voice to use (default: af_heart)")
    parser.add_argument("--speed", type=float, default=1.0, help="Speech speed (default: 1.0)")
    parser.add_argument("--no-play", action="store_true", help="Don't play the audio, just generate it")
    parser.add_argument("--format", choices=["wav", "mp3"], default="wav", help="Output format (default: wav)")
    parser.add_argument("--output", help="Save to specific output file instead of a temporary file")
    parser.add_argument("--no-stream", action="store_true", help="Don't stream audio as it's generated (wait until all is ready)")
    
    # Parse arguments
    args = parser.parse_args()
    
    if not args.text:
        print(colored(__doc__, "yellow"))
        return
    
    # Run async main
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    return asyncio.run(async_main(args))

if __name__ == "__main__":
    sys.exit(main() or 0) 