"""Bounded local TTS; AI-generated wording is accepted as plain text."""
import asyncio
import uuid
from pathlib import Path

import av

from handler import MessageTrack


def convert(source: Path, target: Path):
    with av.open(str(source)) as reader, av.open(str(target), mode='w', format='wav') as writer:
        output = writer.add_stream('pcm_s16le', rate=48000)
        output.layout = 'mono'
        resampler = av.AudioResampler(format='s16', layout='mono', rate=48000)
        for frame in reader.decode(audio=0):
            for converted in resampler.resample(frame):
                for packet in output.encode(converted):
                    writer.mux(packet)
        for converted in resampler.resample(None):
            for packet in output.encode(converted):
                writer.mux(packet)
        for packet in output.encode(None):
            writer.mux(packet)
    MessageTrack(target).stop()


async def synthesize(root, text):
    if not isinstance(text, str) or len(text) > 400 or '\x00' in text:
        raise ValueError('Message must be at most 400 characters')
    if not text.strip():
        return None
    name = f'message-{uuid.uuid4().hex}'
    source, target = root / f'{name}.source.wav', root / f'{name}.wav'
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            'espeak-ng', '-v', 'en-gb', '-s', '155', '-w', str(source), '--stdin',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(15):
            await process.communicate(text.encode())
        if process.returncode:
            raise RuntimeError('Speech synthesis failed')
        await asyncio.to_thread(convert, source, target)
        return target.name
    except BaseException:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        target.unlink(missing_ok=True)
        raise
    finally:
        source.unlink(missing_ok=True)
