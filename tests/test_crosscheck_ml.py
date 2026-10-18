import io,json
import pytest
Image=pytest.importorskip('PIL.Image')
from app.ml.video import Frame
from ml_tools.crosscheck import CrosscheckModel


def test_refinement_preserves_every_source_timestamp_with_separate_images():
    raw=io.BytesIO();Image.new('RGB',(848,480),'blue').save(raw,format='JPEG')
    frames=[Frame(i*0.7,raw.getvalue()) for i in range(28)]
    captured=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,size): return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"assessable":true,"events":[]}'}}]}).encode()
    class Opener:
        def open(self,request,**kwargs):
            captured.append(json.loads(request.data));return Response()
    model=CrosscheckModel();model.reviewer.opener=Opener()
    result=model.infer(frames,10,refine=True,rule_ids=('R22',))
    assert result.events==[]
    parts=captured[0]['messages'][1]['content']
    assert sum(p['type']=='image_url' for p in parts)==28
    assert any(p.get('text')=='Frame 27, original timestamp 18.900 s' for p in parts)
    assert not any(p['type']=='input_video' for p in parts)
    assert all(f.jpeg==raw.getvalue() for f in frames)
