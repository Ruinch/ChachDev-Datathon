"""Research-only video screening / separate-image verification ablation.

Changing input representation may expose unstable claims, but the same underlying
model is not an independent judge. Original frames/timestamps are never mutated;
only temporary inference inputs are resized to fit the pinned context capacity.
"""
from __future__ import annotations
import io
import time
from app.ml.model import LoopbackModel
from app.ml.video import Frame,check_deadline


class CrosscheckModel(LoopbackModel):
    def __init__(self,endpoint='http://127.0.0.1:8091'):
        super().__init__(endpoint,input_mode='video')
        self.reviewer=LoopbackModel(endpoint,input_mode='images')
        self.audit=[]

    def infer(self,frames,timeout,*,refine=False,rule_ids=None):
        if not refine:
            return super().infer(frames,timeout,rule_ids=rule_ids)
        from PIL import Image
        deadline=time.monotonic()+timeout
        resized=[]
        for frame in frames:
            check_deadline(deadline)
            with Image.open(io.BytesIO(frame.jpeg)) as image:
                image=image.convert('RGB')
                image.thumbnail((512,512))
                stream=io.BytesIO();image.save(stream,format='JPEG',quality=90)
                resized.append(Frame(frame.timestamp,stream.getvalue()))
        result=self.reviewer.infer(resized,check_deadline(deadline),refine=True,rule_ids=rule_ids)
        self.audit.append({'refine':True,'input_frames':len(resized),'events':len(result.events),'assessable':result.assessable})
        return result
