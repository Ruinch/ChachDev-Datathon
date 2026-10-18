"""Compare private evaluations; emit aggregates, never video IDs or observations.

Wilson intervals describe sampling uncertainty under independent Bernoulli trials.
Unverified related camera/session groups and repeated tuning violate that ideal;
these intervals are descriptive, not a certification or per-event confidence.
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
from ml_tools.evaluate import summarize


def wilson_interval(successes:int,total:int):
    if not 0<=successes<=total: raise ValueError('Invalid count')
    if not total: return None
    z=1.959963984540054
    p=successes/total
    d=1+z*z/total
    center=(p+z*z/(2*total))/d
    radius=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/d
    return [round(max(0,center-radius),6),round(min(1,center+radius),6)]


def index(rows):
    result={}
    for row in rows:
        if row.get('split')=='quarantine': continue
        if row['sha256'] in result: raise ValueError('Duplicate source would inflate confidence')
        result[row['sha256']]=row
    return result


def with_intervals(rows):
    s=summarize(rows)
    positive=sum(r['label']=='violation' for r in rows)
    negative=len(rows)-positive
    s['descriptive_wilson95']={
        'correct_over_all':wilson_interval(s['tp']+s['tn'],len(rows)),
        'positive_prediction_precision':wilson_interval(s['tp'],s['tp']+s['fp']),
        'recall_over_all_positives':wilson_interval(s['tp'],positive),
        'false_alarm_over_all_negatives':wilson_interval(s['fp'],negative),
        'coverage':wilson_interval(len(rows)-s['failed'],len(rows)),
    }
    return s


def compare_reports(baseline:dict,candidate:dict):
    a=index(baseline['rows']); b=index(candidate['rows'])
    if set(a)!=set(b): raise ValueError('Evaluation source hashes differ')
    if any(a[k]['label']!=b[k]['label'] for k in a): raise ValueError('Reference labels changed')
    if any(a[k].get('point_sec')!=b[k].get('point_sec') for k in a):
        raise ValueError('Reference time points changed')
    improved=regressed=same=changed=0
    for k,x in a.items():
        y=b[k]
        ca=x['status']=='done' and x['verdict']==x['label']
        cb=y['status']=='done' and y['verdict']==y['label']
        improved+=not ca and cb
        regressed+=ca and not cb
        same+=ca==cb
        changed+=(x['status'],x.get('verdict'))!=(y['status'],y.get('verdict'))
    discordant=improved+regressed
    p=min(1.,2*sum(math.comb(discordant,k) for k in range(min(improved,regressed)+1))/2**discordant) if discordant else 1.
    return {'baseline':with_intervals(list(a.values())),'candidate':with_intervals(list(b.values())),
            'paired':{'improved':improved,'regressed':regressed,'unchanged_correctness':same,
                      'changed_status_or_verdict':changed,'exact_mcnemar_p':round(p,6)},
            'limitations':['Binary correctness does not establish event type, timing, or legal applicability.',
                           'Failures and abstentions are not correct negatives.',
                           'Intervals assume independent samples; unverified session groups and tuning limit generalization.',
                           'Same-model agreement is repeatability, not independent confirmation or calibrated confidence.']}


def repeatability(first:dict,second:dict,predictions_a:dict,predictions_b:dict,tolerance_sec=5):
    compare_reports(first,second)  # Enforce identical sources/reference labels and time points.
    a=index(first['rows']); b=index(second['rows'])
    stable=correct=wrong=both_completed=0
    def match_events(left,right):
        if len(left)!=len(right): return False
        assigned={}
        def assign(i,seen):
            for j,y in enumerate(right):
                x=left[i]
                if j in seen or x['rule_id']!=y['rule_id'] or abs(x['timecode_sec']-y['timecode_sec'])>tolerance_sec: continue
                seen.add(j)
                if j not in assigned or assign(assigned[j],seen):
                    assigned[j]=i
                    return True
            return False
        return all(assign(i,set()) for i in range(len(left)))
    for key,x in a.items():
        y=b[key]
        if x['status']!='done' or y['status']!='done': continue
        both_completed+=1
        left=predictions_a[x['id']]['violations']; right=predictions_b[y['id']]['violations']
        if x['verdict']==y['verdict'] and match_events(left,right):
            stable+=1
            correct+=x['verdict']==x['label']
            wrong+=x['verdict']!=x['label']
    return {'paired_videos':len(a),'both_completed':both_completed,'stable_completed_predictions':stable,
            'stable_and_binary_correct':correct,'stable_but_binary_wrong':wrong,
            'event_time_agreement_tolerance_sec':tolerance_sec,
            'meaning':'Agreement of model outputs, not ground-truth localization or calibrated confidence.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('baseline',type=Path); p.add_argument('candidate',type=Path)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists(): raise SystemExit('Preserve previous report: choose fresh output')
    result=compare_reports(json.loads(args.baseline.read_text(encoding='utf-8')),json.loads(args.candidate.read_text(encoding='utf-8')))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True)

if __name__=='__main__': main()
