"""Local, silent analysis clips; evidence always comes from the original video."""
from __future__ import annotations

import base64
import io
from fractions import Fraction

from app.ml.video import Frame, VideoAnalysisError


def encode_sampled_clip(frames: list[Frame]) -> bytes:
    """Encode one sample per second, preserving the index -> source PTS map externally.

    llama.cpp's native video path uses the model's temporal frame merge; separate
    image_url parts do not. These analysis-only clips must never become evidence.
    No source audio is opened or copied. All inputs are locally decoded JPEGs.
    """
    import av
    from PIL import Image

    if not 2 <= len(frames) <= 48:
        raise VideoAnalysisError('Unsupported number of temporal input frames')
    output = io.BytesIO()
    # llama.cpp decodes from a pipe: a trailing MP4 moov atom would require seeking.
    with av.open(output, 'w', format='mp4', options={
            'movflags': 'frag_keyframe+empty_moov+default_base_moof'}) as container:
        stream = None
        for index, item in enumerate(frames):
            with Image.open(io.BytesIO(item.jpeg)) as image:
                image = image.convert('RGB')
                if stream is None:
                    size = (image.width + image.width % 2, image.height + image.height % 2)
                    stream = container.add_stream('mpeg4', rate=1)
                    stream.width, stream.height = size
                    stream.pix_fmt = 'yuv420p'
                    stream.codec_context.max_b_frames = 0
                    stream.bit_rate = 8_000_000
                if image.width > size[0] or image.height > size[1]:
                    raise VideoAnalysisError('Inconsistent temporal input dimensions')
                canvas = Image.new('RGB', size)
                canvas.paste(image, (0, 0))
                frame = av.VideoFrame.from_image(canvas)
                frame.pts, frame.time_base = index, Fraction(1, 1)
                for packet in stream.encode(frame):
                    container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return output.getvalue()


def temporal_content(frames: list[Frame]) -> list[dict]:
    mapping = ', '.join(f'{i}={frame.timestamp:.3f}s' for i, frame in enumerate(frames))
    return [
        {'type': 'text', 'text': 'This is a silent sampled sequence, one sample per clip second. '
         'Clip second N identifies frame N, NOT original elapsed time. '
         'Use these exact original timestamp mappings: ' + mapping + '. '
         'Return frame indices, not original seconds. Do not infer action speed from this sampled clip.'},
        {'type': 'input_video', 'input_video': {'data': 'data:video/mp4;base64,' +
         base64.b64encode(encode_sampled_clip(frames)).decode('ascii')}},
    ]
