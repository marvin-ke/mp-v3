"""Offline recognition of rotated Chinese click targets. No HTTP or browser access."""
import hashlib
import math
import time
from pathlib import Path
import cv2
import numpy as np
import onnxruntime as ort

MODEL_PATH=Path('/config/custom-site-login/models/ppocrv5-mobile.onnx')
MODEL_SHA256='5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5'

class ClickTextRecognizer:
    def __init__(self, model_path=MODEL_PATH):
        path=Path(model_path)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=MODEL_SHA256:
            raise ValueError('click recognition model missing or checksum mismatch')
        options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
        self.engine=ort.InferenceSession(str(path),sess_options=options,providers=['CPUExecutionProvider'])
        self.characters=['blank']+self.engine.get_modelmeta().custom_metadata_map['character'].splitlines()+[' ']

    @staticmethod
    def _masks(image, header=False):
        masks=[];seen=set()
        def add(mask):
            area=(mask>0).mean()
            if np.count_nonzero(mask)<25 or not .025<=area<=.65:return
            key=mask.tobytes()
            if key not in seen:seen.add(key);masks.append(mask)
        if header:
            hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
            for center in range(0,180,10):
                distance=np.abs(hsv[:,:,0].astype(float)-center)
                for width in (8,12,16):
                    add(((np.minimum(distance,180-distance)<=width)&(hsv[:,:,1]>65)&(hsv[:,:,2]<245)).astype(np.uint8)*255)
        lab=cv2.cvtColor(image,cv2.COLOR_BGR2LAB)
        # Deterministic clustering: single-threaded isolated worker, no global OCR service.
        cv2.setRNGSeed(42)
        _,labels,_=cv2.kmeans(lab.reshape(-1,3).astype('float32'),4,None,
            (cv2.TERM_CRITERIA_MAX_ITER+cv2.TERM_CRITERIA_EPS,40,.2),5,cv2.KMEANS_PP_CENTERS)
        labels=labels.reshape(image.shape[:2])
        for bits in range(1,15):
            mask=np.isin(labels,[i for i in range(4) if bits&(1<<i)]).astype(np.uint8)*255
            border=np.concatenate([mask[0],mask[-1],mask[:,0],mask[:,-1]])
            if (border>0).mean()<.38:add(mask)
        return masks

    @staticmethod
    def _tensor(mask, angle):
        h,w=mask.shape;side=max(h,w)*2
        canvas=np.zeros((side,side),np.uint8);y=(side-h)//2;x=(side-w)//2;canvas[y:y+h,x:x+w]=mask
        matrix=cv2.getRotationMatrix2D((side/2,side/2),angle,1)
        canvas=cv2.warpAffine(canvas,matrix,(side,side));ys,xs=np.where(canvas>60)
        if len(xs)<10:return None
        image=255-canvas[ys.min():ys.max()+1,xs.min():xs.max()+1]
        image=cv2.copyMakeBorder(image,3,3,3,3,cv2.BORDER_CONSTANT,value=255)
        h,w=image.shape;width=min(96,math.ceil(48*w/h));image=cv2.resize(image,(width,48))
        image=np.repeat(image[None,:,:],3,axis=0).astype('float32')/127.5-1
        tensor=np.zeros((3,48,96),np.float32);tensor[:,:,:width]=image
        return tensor

    def _infer(self, jobs, deadline):
        rows=[]
        for start in range(0,len(jobs),24):
            if time.monotonic()>=deadline:raise TimeoutError('offline recognition deadline')
            batch=jobs[start:start+24]
            output=self.engine.run(None,{'x':np.stack([j[0] for j in batch])})[0]
            for prediction,(_,variant,angle) in zip(output,batch):
                ids=prediction.argmax(1);confidence=prediction.max(1)
                take=[i for i,v in enumerate(ids) if v!=0 and (i==0 or v!=ids[i-1])]
                text=''.join(self.characters[ids[i]] for i in take)
                if len(text)==1 and '\u3400'<=text<='\u9fff':
                    rows.append((text,float(confidence[take].mean()),variant,angle))
        return rows

    @staticmethod
    def _support(rows):
        """Require the same glyph on adjacent orientations; a peak alone is insufficient."""
        scores={}
        for text,score,variant,angle in rows:
            for other,value,other_variant,other_angle in rows:
                delta=abs(angle-other_angle)%360;delta=min(delta,360-delta)
                if text==other and variant==other_variant and 0<delta<=15:
                    scores[text]=max(scores.get(text,0),min(score,value))
        return scores

    def _read(self, crops, header, deadline, upscale=False):
        masks=[];seen=set()
        for crop in crops:
            for mask in self._masks(crop,header):
                key=(mask.shape,mask.tobytes())
                if key not in seen:seen.add(key);masks.append(mask)
        if upscale:
            # Rotate at a larger resolution so small glyph strokes survive resampling.
            masks=[cv2.resize(mask,None,fx=3,fy=3,interpolation=cv2.INTER_NEAREST) for mask in masks]
        jobs=[]
        for variant,mask in enumerate(masks):
            for angle in range(-180,180,10):
                tensor=self._tensor(mask,angle)
                if tensor is not None:jobs.append((tensor,variant,angle))
        rows=self._infer(jobs,deadline)
        refine=set()
        for text,score,variant,angle in rows:
            if score>=.65:
                for offset in (-5,5):refine.add((variant,(angle+offset+180)%360-180))
        jobs=[]
        for variant,angle in sorted(refine):
            tensor=self._tensor(masks[variant],angle)
            if tensor is not None:jobs.append((tensor,variant,angle))
        rows.extend(self._infer(jobs,deadline))
        return self._support(rows)

    def recognize(self, header, background, boxes, regions, timeout=65):
        start=time.monotonic();deadline=start+timeout
        result={'accepted':False,'method':'ppocrv5-color-rotation','selected':[],'labels':[],
                'target_scores':[],'candidate_scores':[]}
        if header is None or background is None or not 2<=len(regions)<=6 or not len(regions)<=len(boxes)<=12:
            return dict(result,reason='提示或背景目标数量不可靠')
        try:
            h,w=header.shape[:2];regions=sorted(regions)
            if any(not (0<=a<c<=w and 0<=b<d<=h) for a,b,c,d in regions):return dict(result,reason='提示区域无效')
            # Use actual target order, but preserve FULL-height cells as well as tight crops.
            # Morphological component boxes can discard thin or detached glyph strokes.
            boundaries=[0]+[(regions[i-1][2]+regions[i][0])//2 for i in range(1,len(regions))]+[w]
            for i,(x,y,r,b) in enumerate(regions):
                crops=[header[y:b,x:r],header[:,boundaries[i]:boundaries[i+1]]]
                scores=self._read(crops,True,deadline)
                ranked=sorted(scores.items(),key=lambda p:p[1],reverse=True)
                if not ranked or ranked[0][1]<.90 or len(ranked)>1 and ranked[0][1]-ranked[1][1]<.12:
                    refined=self._read(crops,True,deadline,upscale=True)
                    refined=sorted(refined.items(),key=lambda p:p[1],reverse=True)
                    if ranked and ranked[0][1]>=.80 and refined and refined[0][0]!=ranked[0][0]:
                        return dict(result,reason='原图与放大图的提示字识别不一致')
                    ranked=refined
                if not ranked or ranked[0][1]<.90 or len(ranked)>1 and ranked[0][1]-ranked[1][1]<.12:
                    return dict(result,reason='提示字识别或相邻角度一致性不足')
                result['labels'].append(ranked[0][0]);result['target_scores'].append(round(ranked[0][1],4))
            bh,bw=background.shape[:2];all_scores=[]
            for box in boxes:
                if len(box)!=4:return dict(result,reason='背景区域无效')
                x,y,r,b=map(int,box)
                if not (0<=x<r<=bw and 0<=y<b<=bh) or min(r-x,b-y)<8:return dict(result,reason='背景区域越界')
                all_scores.append(self._read([background[y:b,x:r]],False,deadline))
            for label in result['labels']:
                scores=[s.get(label,0) for s in all_scores];ranked=sorted(range(len(scores)),key=lambda i:scores[i],reverse=True)
                chosen=ranked[0]
                if scores[chosen]<.90 or scores[chosen]-scores[ranked[1]]<.15 or chosen in result['selected']:
                    return dict(result,reason='背景字匹配不足或存在歧义')
                alternatives=[value for text,value in all_scores[chosen].items() if text!=label]
                if alternatives and scores[chosen]-max(alternatives)<.12:
                    x,y,r,b=map(int,boxes[chosen])
                    refined=self._read([background[y:b,x:r]],False,deadline,upscale=True)
                    alternative=max((value for text,value in refined.items() if text!=label),default=0)
                    score=refined.get(label,0)
                    other=max((value for index,value in enumerate(scores) if index!=chosen),default=0)
                    if score<.90 or score-alternative<.12 or score-other<.15:
                        return dict(result,reason='放大复核后背景字仍存在歧义')
                    scores[chosen]=score
                    all_scores[chosen]=refined
                result['selected'].append(chosen);result['candidate_scores'].append(round(scores[chosen],4))
            return dict(result,accepted=True,reason='完整提示字与背景字唯一匹配',seconds=round(time.monotonic()-start,3))
        except TimeoutError:return dict(result,reason='同一题离线识别超时，未刷新或提交')
