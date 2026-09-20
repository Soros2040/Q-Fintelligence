"""The common installed CLI; calculations delegate to the public Python API."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from .api import clean, load_json, write_json


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {'verify', 'demo', 'analyze', 'recompute'}:
        from .compat_cli import main as old_main
        return old_main(argv)
    parser = argparse.ArgumentParser(prog='qf-algorithm', description='量融智枢算法软件 2.1.0')
    parser.add_argument('--version', action='version', version='qf-algorithm 2.1.0')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run', help='执行规范化数据或冻结数据的完整本地方法链')
    run.add_argument('--config', type=Path)
    run.add_argument('--input', type=Path)
    run.add_argument('--frozen', action='store_true', default=None)
    run.add_argument('--data-root', type=Path)
    run.add_argument('--output-dir', type=Path)
    run.add_argument('--run-id')
    run.add_argument('--seed', type=int)
    run.add_argument('--shots', type=int)
    run.add_argument('--epochs', type=int)
    run.add_argument('--risk-mode', choices=['factor', 'shrinkage', 'quantum_specific'])
    run.add_argument('--risk-lambda', type=float)
    run.add_argument('--cost', type=float)
    run.add_argument('--qaoa-gamma', type=float)
    run.add_argument('--qaoa-beta', type=float)
    run.add_argument('--qaoa-mode', choices=['fixed', 'optimize'])
    norm = sub.add_parser('normalize', help='校验/输出规范化输入')
    norm.add_argument('--input', type=Path)
    norm.add_argument('--frozen', action='store_true')
    norm.add_argument('--data-root', type=Path)
    norm.add_argument('--output', type=Path, required=True)
    select = sub.add_parser('select', help='拟合本地学习代理或校验受约束候选决定')
    select.add_argument('--input', type=Path, required=True)
    select.add_argument('--decision', type=Path)
    select.add_argument('--output', type=Path, required=True)
    audit = sub.add_parser('audit-receipt', help='离线回执结构与已有合同检查')
    audit.add_argument('--receipt', type=Path, required=True)
    audit.add_argument('--batch', type=Path)
    audit.add_argument('--submit-receipt', type=Path)
    stats = sub.add_parser('statistics', help='从归档端点复算15项主比较')
    stats.add_argument('--data-root', type=Path)
    stats.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'run':
        from .pipeline import run_pipeline
        from .config import RunConfig
        options = RunConfig.from_json(args.config).as_kwargs() if args.config else RunConfig().as_kwargs()
        for name in ['frozen', 'data_root', 'output_dir', 'run_id', 'seed', 'shots', 'epochs', 'risk_mode',
                     'risk_lambda', 'cost', 'qaoa_gamma', 'qaoa_beta', 'qaoa_mode']:
            if getattr(args, name) is not None:
                options[name] = getattr(args, name)
        if args.input is not None:
            options['input_data'] = args.input
        result = run_pipeline(**RunConfig.from_mapping(options).as_kwargs())
        result = {k: result[k] for k in ['output_dir', 'scientificStatus', 'executionMode', 'summary']}
    elif args.command == 'normalize':
        from .data import load_panel
        if args.output.exists():
            raise FileExistsError('Choose a new normalized output file')
        document = load_panel(args.input, frozen=args.frozen, data_root=args.data_root)
        write_json(args.output, document)
        result = {'status': 'PASS', 'dates': len(document['dates']), 'assets': document['asset_order'], 'output': args.output}
    elif args.command == 'select':
        from .selection import LearningProxy, build_request, validate_decision
        document = load_json(args.input)
        if args.output.exists():
            raise FileExistsError('Choose a new selection output file')
        if args.decision:
            result = {'status': 'PASS', 'decision': validate_decision(load_json(args.decision), document), 'external_calls': 0}
        else:
            count = document.get('training_count', len(document['targets']))
            proxy = LearningProxy().fit(document['feature_rows'][:count], document['targets'][:count])
            scores = proxy.predict(document['feature_rows'])
            result = build_request(document['candidate_ids'], scores, run_id=document['run_id'], data_epoch=document['data_epoch'])
            proxy.save(args.output.with_suffix('.model.json'))
        write_json(args.output, result)
    elif args.command == 'statistics':
        from .analysis import recompute_primary_statistics
        result = recompute_primary_statistics(args.data_root, output_dir=args.output_dir)
        result = {key: result[key] for key in ['status', 'comparison_count', 'maximum_numeric_error', 'external_calls', 'scope']}
    else:
        from .governance import audit_receipt
        result = audit_receipt(args.receipt, batch_path=args.batch, submit_receipt_path=args.submit_receipt)
    print(json.dumps(clean(result), ensure_ascii=False, indent=2, allow_nan=False))
    return 0


def entrypoint():
    try:
        return main()
    except (ValueError, FileNotFoundError, FileExistsError, RuntimeError) as exc:
        print(json.dumps({'status': 'ERROR', 'type': type(exc).__name__, 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
