#!/usr/bin/env python3
"""
Unit tests for the speak.py module.
Run with: python -m unittest test_speak
"""
import unittest
import os
import tempfile
from unittest.mock import patch, MagicMock
import speak


class TestKokoroTTS(unittest.TestCase):
    """Test cases for KokoroTTS class."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Mock the KPipeline class to avoid external dependencies
        self.pipeline_patcher = patch('speak.KPipeline')
        self.mock_pipeline = self.pipeline_patcher.start()
        
        # Create a temporary directory for test files
        self.temp_dir = tempfile.mkdtemp()
    
    def tearDown(self):
        """Tear down test fixtures."""
        self.pipeline_patcher.stop()
        
        # Clean up temporary files
        for file in os.listdir(self.temp_dir):
            os.unlink(os.path.join(self.temp_dir, file))
        os.rmdir(self.temp_dir)
    
    @patch('speak.play_audio')
    async def test_stream_and_save(self, mock_play_audio):
        """Test the stream_and_save method."""
        # Mock the pipeline instance
        mock_pipeline_instance = MagicMock()
        self.mock_pipeline.return_value = mock_pipeline_instance
        
        # Set up the pipeline to return sample audio data
        import numpy as np
        sample_audio = np.zeros(1000, dtype=np.float32)
        mock_pipeline_instance.return_value.__iter__.return_value = [
            ('graphemes1', 'phonemes1', sample_audio),
            ('graphemes2', 'phonemes2', sample_audio)
        ]
        
        # Create an instance of KokoroTTS
        tts = speak.KokoroTTS()
        
        # Test with WAV output
        output_file = os.path.join(self.temp_dir, "test_output.wav")
        result = await tts.stream_and_save(
            "Test text",
            voice="test_voice",
            speed=1.0,
            output_file=output_file,
            format="wav",
            play=True,
            no_stream=True  # Use no_stream for easier testing
        )
        
        # Verify the output file was created
        self.assertEqual(result, output_file)
        self.assertTrue(os.path.exists(output_file))
        
        # Verify play_audio was called
        mock_play_audio.assert_called_once()


class TestMainFunctionality(unittest.TestCase):
    """Test cases for the main functionality."""
    
    @patch('speak.asyncio.run')
    @patch('speak.argparse.ArgumentParser.parse_args')
    def test_main_function(self, mock_parse_args, mock_asyncio_run):
        """Test the main function."""
        # Mock command line arguments
        mock_args = MagicMock()
        mock_args.text = "Test text"
        mock_args.voice = "af_heart"
        mock_args.speed = 1.0
        mock_args.no_play = False
        mock_args.format = "wav"
        mock_args.output = None
        mock_args.no_stream = False
        mock_parse_args.return_value = mock_args
        
        # Run the main function
        speak.main()
        
        # Verify asyncio.run was called
        mock_asyncio_run.assert_called_once()


if __name__ == '__main__':
    unittest.main() 