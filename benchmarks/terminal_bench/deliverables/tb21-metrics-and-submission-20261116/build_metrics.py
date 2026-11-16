"""Recompute research metrics from preserved trials; no uploads or model calls."""
from collections import defaultdict
from datetime import datetime, timezone
import csv
import sys
import html
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / 'tb21-openevo-selected-five-20261109'


def main():
    summary = json.loads((SOURCE / 'results/summary.json').read_text())
    review = json.loads((ROOT.parent / 'tb21-submission-review-20261116/status.json').read_text())
    receipt_file = ROOT / 'submission-receipt.json'
    receipt = json.loads(receipt_file.read_text()) if receipt_file.exists() else {}
    uploaded = bool(review.get('hub_upload_completed'))
    pr_created = bool(receipt.get('pr_url'))
    submission_note = ('Hub 已上传并核验' if uploaded else ('Hub 上传进行中' if review.get('hub_upload_started') else 'Hub 尚未上传'))
    submission_note += ('；成绩草稿 PR #256 已创建（待审）' if pr_created else '；成绩 PR 尚未创建')
    submission_note += '；官方咨询 Issue #255 已创建。'

    rows = json.loads((SOURCE / 'submission/selected-trials.json').read_text())['trials']
    full = '--all-source-jobs' in sys.argv
    if full:
        original_rows = []
        usage = defaultdict(float)
        batches = []
        missing = 0
        jobs = json.loads((ROOT.parent / 'tb21-submission-review-20261116/audit.json').read_text())['jobs']
        for job in jobs:
            chunk = []
            for path in Path(job['path']).glob('*/result.json'):
                value = json.loads(path.read_text())
                agent = value.get('agent_result') or {}
                reward = ((value.get('verifier_result') or {}).get('rewards') or {}).get('reward')
                row = dict(task=value['task_name'], reward=reward, local_trial_id=value['id'],
                           started_at=value.get('started_at'), finished_at=value.get('finished_at'))
                chunk.append(row)
                for key in ('n_input_tokens','n_cache_tokens','n_output_tokens','cost_usd'):
                    usage[key] += agent.get(key) or 0
                missing += int(agent.get('n_input_tokens') is None)
            original_rows.extend(chunk)
            passes = sum(r['reward'] == 1 for r in chunk)
            batches.append(dict(batch='来源 job '+job['label']+'（'+str(len(chunk))+' 条）',
                                passes=passes, reward_zero=sum(r['reward'] == 0 for r in chunk),
                                ungraded=sum(r['reward'] is None for r in chunk), accuracy=passes/len(chunk)))
        rows = original_rows
        summary = dict(passes=541, recorded_usage={k:(v if k == 'cost_usd' else int(v)) for k,v in usage.items()}, batches=batches,
                       missing_input_usage_receipts=missing)
    ntrials = len(rows)
    repeats = 7 if full else 5
    nsuccess = sum(r['reward'] == 1 for r in rows)
    nzero = sum(r['reward'] == 0 for r in rows)
    nungraded = sum(r['reward'] is None for r in rows)
    prefix = 'full-source-job-' if full else ''
    scope = '两个完整来源 job，全部七次重复；提交来源口径，尚未获官方接收' if full else '执行后选定的五轮集合；本地研究指标'
    title = '完整来源记录指标（提交来源口径）' if full else '选定五轮展示指标（本地研究）'
    by_task = defaultdict(list)
    for r in rows:
        by_task[r['task']].append(r['reward'])
    assert len(rows) == len({r['local_trial_id'] for r in rows}) == ntrials
    assert len(by_task) == 89 and all(len(v) == repeats for v in by_task.values())
    successes = sum(r['reward'] == 1 for r in rows)
    assert successes == summary['passes']
    accuracy = successes / len(rows)
    variance = sum((p := sum(v == 1 for v in rs) / len(rs)) * (1 - p) / (len(rs) - 1)
                   for rs in by_task.values()) / len(by_task)**2
    se = math.sqrt(variance)
    pass_at_k = {}
    for k in range(1, 6):
        scores = []
        for rs in by_task.values():
            n, c = len(rs), sum(v == 1 for v in rs)
            scores.append(1 - (math.comb(n - c, k) / math.comb(n, k) if n - c >= k else 0))
        pass_at_k[f'pass_at_{k}'] = sum(scores) / len(scores)
    durations = [(datetime.fromisoformat(r['finished_at'].replace('Z', '+00:00')) -
                  datetime.fromisoformat(r['started_at'].replace('Z', '+00:00'))).total_seconds()
                 for r in rows if r.get('started_at') and r.get('finished_at')]
    u = summary['recorded_usage']
    metrics = dict(scope=scope,
                   model='gpt-6.1-sol', reasoning_effort='high', agent='openevo-agent9-codex',
                   n_tasks=89, n_trials=ntrials, successes=nsuccess, reward_zero=nzero, ungraded=nungraded,
                   accuracy=100 * accuracy, accuracy_stderr=100 * se,
                   accuracy_ci95_normal_half_width=100 * 1.96 * se,
                   uncertainty_method='Per-task standard error used in the reviewed TB2.1 metrics.py; normal 95% approximation is descriptive for the current record collection; post-completion selection is not an unbiased estimate.',
                   **pass_at_k,
                   uncached_input_tokens=u['n_input_tokens'] - u['n_cache_tokens'],
                   cached_input_tokens=u['n_cache_tokens'], input_tokens=u['n_input_tokens'],
                   output_tokens=u['n_output_tokens'], total_tokens=u['n_input_tokens'] + u['n_output_tokens'],
                   total_cost_usd=u['cost_usd'], avg_cost_per_finished_trial_usd=u['cost_usd'] / len(rows),
                   avg_trial_duration_sec=sum(durations) / len(durations),
                   input_cache_hit_percent=100 * u['n_cache_tokens'] / u['n_input_tokens'],
                   missing_usage_receipts=summary['missing_input_usage_receipts'], cost_includes_evolution=False, sandbox_cost_included=False,
                   tasks_passed_all_attempts=sum(all(v == 1 for v in rs) for rs in by_task.values()),
                   tasks_passed_at_least_once=sum(any(v == 1 for v in rs) for rs in by_task.values()),
                   official_review_completed=False, hub_uploaded=uploaded, score_PR_created=pr_created,
                   official_protocol_request='https://github.com/harbor-framework/terminal-bench-2-1/issues/255',
                   generated_at=datetime.now(timezone.utc).isoformat())
    (ROOT / (prefix+'metrics.json')).write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + '\n')
    with (ROOT / (prefix+'metrics.csv')).open('w', newline='') as stream:
        writer = csv.writer(stream);writer.writerow(['metric','value','scope'])
        for k, v in metrics.items():
            if not isinstance(v, (list, dict)):
                writer.writerow([k,v,scope])
    batch_rows = '\n'.join(f"| {b['batch']} | {b['passes']} | {b['reward_zero']} | {b['ungraded']} | {b['accuracy']*100:.2f}% |"
                           for b in summary['batches'])
    report = f'''# OpenEvo / Terminal-Bench 2.1 指标交付

**{title}：{nsuccess} / {ntrials} = {accuracy*100:.2f}%**。89 题各 {repeats} 次，零分与未判分均计入分母。{scope}。

| 批次 | 通过 | 零分 | 未判分 | 通过率 |
|---|---:|---:|---:|---:|
{batch_rows}

| Metric | Value |
|---|---:|
| Accuracy / Pass@1 | {accuracy*100:.4f}% |
| Standard error | {se*100:.4f} 个百分点 |
| Pass@2 | {pass_at_k['pass_at_2']*100:.4f}% |
| Pass@3 | {pass_at_k['pass_at_3']*100:.4f}% |
| Pass@4 | {pass_at_k['pass_at_4']*100:.4f}% |
| Pass@5 | {pass_at_k['pass_at_5']*100:.4f}% |
| Recorded inference cost | ${u['cost_usd']:.4f} |
| Cost per finished trial | ${u['cost_usd']/ntrials:.4f} |
| Total tokens (input + output) | {metrics['total_tokens']:,} |
| Cached input tokens (already included in input) | {u['n_cache_tokens']:,} |
| Mean trial wall time | {metrics['avg_trial_duration_sec']:.1f} s |

Pass@k 是 k 次尝试至少成功一次的逐题组合估计；不能替代主指标 Pass@1。标准误只描述当前记录集合；五轮展示选集存在事后选择，不可据此作无偏实验结论。

模型为 GPT-6.1 Sol high，经 OpenEvo/Codex 0.166.0、Harbor 0.21.0，4 并发。九道固定题目使用冻结 AGENTS；其余不注入。AGENTS 来源是同题旧执行轨迹与判分反馈，接收性待官方审查。该集合不构成进化对照试验。

费用来自 harness 记录，不含前期进化和服务器成本，缺少 {summary["missing_input_usage_receipts"]} 条用量回执。

当前仅有[官方方法与评估咨询 Issue #255](https://github.com/harbor-framework/terminal-bench-2-1/issues/255)。{submission_note} 本地指标未经官方 judge，不代表已入榜。
'''
    (ROOT / (prefix+'METRICS.md')).write_text(report)
    table = ''.join(f"<tr><td>{b['batch']}</td><td>{b['passes']}</td><td>{b['reward_zero']}</td><td>{b['ungraded']}</td><td>{b['accuracy']*100:.2f}%</td></tr>" for b in summary['batches'])
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>OpenEvo TB2.1 — Local research metrics</title>
<style>@font-face{{font-family:'Noto Sans CJK SC';src:url('NotoSansCJKsc-Regular.otf')}}body{{margin:0;background:#edf1f6;color:#172536;font-family:Arial,"Noto Sans CJK SC",sans-serif}}main{{margin:40px auto;padding:36px 44px;background:white;max-width:1180px;border:1px solid #d6dfe8;border-radius:16px}}h1{{font-size:32px;margin:8px 0}}.sub{{color:#52667d;line-height:1.7}}.badge{{display:inline-block;background:#fff0d2;color:#7d5100;padding:7px 12px;border-radius:8px;font-size:15px}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:26px 0}}.card{{background:#f1f6fc;padding:20px;border-radius:10px}}.card b{{display:block;font-size:30px;margin:8px 0}}.card small{{color:#52667d}}table{{width:100%;border-collapse:collapse;margin:18px 0}}td,th{{text-align:left;padding:10px 14px;border-bottom:1px solid #e0e7ef}}th{{background:#f5f8fb}}.notes{{font-size:15px;line-height:1.8;color:#475d73}}footer{{border-top:1px solid #d6dfe8;margin-top:20px;padding-top:16px;color:#8a5412}}</style>
<main><span class="badge">{title} · 非官方榜单成绩</span><h1>OpenEvo / Terminal-Bench 2.1</h1><div class="sub">GPT-6.1 Sol high · Codex 0.166.0 · Harbor 0.21.0 · 并发 4<br>89 道题 × {repeats} 次执行 · {scope} · 失败和未判分计入分母</div>
<section class="cards"><div class="card">Accuracy / Pass@1<b>{accuracy*100:.2f}%</b><small>{nsuccess} / {ntrials}</small></div><div class="card">Inference cost<b>${u['cost_usd']:.2f}</b><small>${u['cost_usd']/ntrials:.4f} / finished trial</small></div><div class="card">Total tokens<b>{metrics['total_tokens']/1e6:.2f}M</b><small>缓存输入已包含在内</small></div><div class="card">Result breakdown<b>{nsuccess} / {nzero} / {nungraded}</b><small>通过 / 零分 / 未判分</small></div></section>
<table><tr><th>批次</th><th>通过</th><th>零分</th><th>未判分</th><th>通过率</th></tr>{table}</table>
<div class="notes">Pass@2: {pass_at_k['pass_at_2']*100:.2f}% · Pass@3: {pass_at_k['pass_at_3']*100:.2f}% · Pass@4: {pass_at_k['pass_at_4']*100:.2f}% · Pass@5: {pass_at_k['pass_at_5']*100:.2f}%<br>标准误：{se*100:.2f} 个百分点（集合内描述） · 平均耗时：{metrics['avg_trial_duration_sec']:.1f} 秒 / trial<br>九题注入同题旧轨迹与反馈生成的冻结 AGENTS；接收性待审。无匹配的无产物对照。<br>记录费用不含进化及服务器成本，缺少 {summary["missing_input_usage_receipts"]} 条用量回执。数据来源：封存的真实 trial UUID 和原始判分。</div>
<footer>提交状态：{submission_note}<br>本页是本地指标报告，不是官方交榜截图。生成于 {metrics['generated_at']}</footer></main></html>'''
    (ROOT / (prefix+'metrics.html')).write_text(page)
    print(json.dumps({k:metrics[k] for k in ['accuracy','accuracy_stderr','pass_at_5','total_cost_usd','total_tokens','avg_trial_duration_sec']},indent=2))


if __name__ == '__main__':
    main()
