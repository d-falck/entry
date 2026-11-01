from pathlib import Path
import tempfile
import unittest
import wave
from speech import convert

class ConversionTests(unittest.TestCase):
    def test_speech_resampled_to_sender_format(self):
        with tempfile.TemporaryDirectory() as temp:
            source,target = Path(temp)/'in.wav',Path(temp)/'out.wav'
            with wave.open(str(source),'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(22050)
                output.writeframes(b'\x01\x00'*2205)
            convert(source,target)
            with wave.open(str(target)) as result:
                self.assertEqual((result.getnchannels(),result.getsampwidth(),result.getframerate()),(1,2,48000))
                self.assertGreater(result.getnframes(),4700)
