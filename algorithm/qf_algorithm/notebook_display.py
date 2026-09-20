"""Chinese research pages backed by saved calculation objects and source code."""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
from IPython.display import HTML, Image, Markdown, display

from .api import digest
from .notebook_audit import (RunView, artifact_inventory, audit_run, compare_runs, portfolio_tables,
                             source_functions)

TITLES = {
    '00': '风险增强组合决策：从行情到计算结果',
    '01': '金融数据：资产轴、决策时间与目标收益',
    '02': '量子读出、收益学习与特定风险估计',
    '03': '组合目标：收益、风险、费用与数学转换',
    '04': '固定持仓数QAOA：从目标到测量分布',
    '05': '候选线路评价与学习代理选择',
    '06': '冻结实验：统计复算与测量档案',
    '07': '冻结行情上的风险建模与线路选择',
}

METHODS = {
    '00': r'''输入行情形成资产特征与风险关系。共享量子映射产生可测读出，图学习头估计收益；协方差与持仓成本共同进入组合目标。经典参照、量子线路质量和代理选择集合分别提供组合、分布和线路层面的计算结果。

计算使用六资产、三资产等权持仓。组合权重为 $w=x/3$，资产轴贯穿输入、协方差、二元状态和持仓解释。''',
    '01': r'''每条样本同时包含信号日期、目标持有区间和标签成熟日期。信号时点为当日收盘，目标收益覆盖下一开盘至随后开盘；训练样本的标签须在评价起点之前已经可见。

规范化数组共享日期轴与六资产轴。收益使用简单收益比例，$0.01$对应$1\%$。特征包含当期收益、多个历史均值和历史波动。''',
    '02': r'''图聚合特征仅用训练样本拟合标准化尺度，再映射为旋转角度。六比特线路输出十一维可测量：六个单比特$Z$与五个相邻$ZZ$期望值。图学习头在这些读出上拟合收益预测。

特定风险学习以收益创新扣除因子投影后的残差平方为目标。协方差重建为 $\Sigma=\Lambda F\Lambda^{\mathsf T}+\operatorname{diag}(d)$；$d$采用正值下限，单位为单期简单收益率方差。''',
    '03': r'''给定预测收益$\mu$、协方差$\Sigma$、前期持仓$w^{-}$、风险权重$\lambda$及成本率$c$，组合目标为

$$f(x)=-\mu^{\mathsf T}w+\lambda w^{\mathsf T}\Sigma w+c\lVert w-w^{-}\rVert_1,\quad w=x/3.$$

在二元变量上将目标展开为QUBO，再以$z_i=1-2x_i$转换为Ising表示。六资产共有64个状态，其中选择三资产的可行组合有20个；逐状态核对三种表达式。''',
    '04': r'''三激发基态经过代价相位和XY混合层，再在计算基测量。保存的QCIS给出实际门序列，分布按$q_5\cdots q_0$书写，最右侧$q_0$对应资产轴第一项。

$$|\psi(\gamma,\beta)\rangle=U_{XY}(\beta)e^{-i\gamma H_C}|000111\rangle.$$

**单层线路的相位性质。** 初态是对角代价算子的本征态，因此首个代价层产生全局相位$e^{-i\gamma E_0}$，它在测量概率中消去。当前单层分布随$\beta$变化；$\gamma$的取值仍进入实际QCIS与运行记录。固定模式直接计算所填角度，优化模式将它们作为初始值。''',
    '05': r'''候选线路先形成本地质量和资源特征。标准化岭回归代理使用四条训练候选拟合，另外两条候选提供留出评价，再对六候选形成预测排序和五候选集合。

$$\widehat y=b+\sum_j\beta_j(x_j-\bar x_j)/s_j.$$

表中同时列出训练角色、观测目标、预测分数、排名和入选状态。优化曲线来自目标函数的实际求值记录，横轴为求值序号，纵轴为同一组合目标。''',
    '06': r'''十五项主比较分别使用原冻结预测、日期误差、持有区间损失和配对目标端点。复算保持各实验的比较对象、统计单位、区组长度、2000次重采样和组内Holm校正。

效应、置信区间与校正$p$值共同呈现统计结果。测量档案另按设备、线路身份、位序及shots连接同线路参照，支持逐项核对实验输入与观测。''',
    '07': r'''本例从冻结行情派生六资产开发窗口，按126期历史观测构建风险关系与协方差。量子特征映射使用已登记的线路参数，图学习头在训练样本上拟合收益，评价样本用于记录预测误差。

预测收益与风险估计进入组合目标，形成经典参照组合与候选线路评价；学习代理利用候选特征生成选择集合。各阶段都保留输入轴、有效配置、实现身份和计算产物。''',
}

LABELS = {'status':'计算状态', 'assets':'资产数量', 'train_dates':'训练日期数',
          'evaluation_dates':'评价日期数', 'sdk_feature_evolutions':'特征态演化次数',
          'risk_minimum_eigenvalue':'风险矩阵最小特征值',
          'quantum_feature_head_mae':'评价集收益预测MAE', 'exact_objective':'穷举参照目标',
          'exact_basket':'穷举参照资产索引', 'qubo_ising_max_error':'目标表示最大误差',
          'candidates':'候选数量', 'shots':'测量次数合计', 'selected_candidates':'代理选择集合',
          'comparison_count':'主比较数量', 'total_shots':'测量次数合计',
          'candidate_count':'候选数量', 'exact_optimum':'穷举参照目标',
          'parameters':'线路参数', 'instance':'基础实例', 'execution':'计算方式',
          'settings':'计算配置数', 'shots_per_setting':'每配置测量次数'}


_CURRENT_TOPIC = None
_PORTABLE_ASSETS = {}


def portable_asset(path, topic, role='DISPLAYED_ARTIFACT'):
    """Save the exact displayed file as a portable content-addressed snapshot."""
    original = Path(path).resolve()
    name = original.name.lower()
    if (not original.is_file() or name == '.env' or name.startswith('.env.')
            or original.suffix.lower() in {'.pem', '.key'}):
        raise ValueError('DISPLAY_ASSET_REGULAR_PUBLIC_FILE_REQUIRED')
    root = Path(os.environ.get('QF_NOTEBOOK_ASSET_ROOT', Path(__file__).resolve().parents[1]/'notebook_assets')).resolve()
    topic = str(topic)[:2]
    if topic not in TITLES:
        raise ValueError('DISPLAY_ASSET_TOPIC_REQUIRED')
    folder = root/topic
    folder.mkdir(parents=True, exist_ok=True)
    checksum = digest(original)
    target = folder/(checksum+'_'+original.name)
    if target.exists():
        if digest(target) != checksum:
            raise ValueError('DISPLAY_ASSET_EXISTING_SHA_MISMATCH')
    else:
        with original.open('rb') as source, target.open('xb') as destination:
            for block in iter(lambda: source.read(1024*1024), b''):
                destination.write(block)
        if digest(target) != checksum or digest(original) != checksum:
            raise ValueError('DISPLAY_ASSET_COPY_SHA_MISMATCH')
    relative = topic+'/'+target.name
    record = {'source_path': str(original), 'source_sha256': checksum,
              'snapshot': relative, 'snapshot_sha256': checksum, 'bytes': target.stat().st_size,
              'role': role, 'transformation': 'BYTE_IDENTICAL_DISPLAY_SNAPSHOT'}
    _PORTABLE_ASSETS.setdefault((str(root), topic), {})[(str(original), checksum)] = record
    return '../notebook_assets/'+quote(relative, safe='/'), record


def _asset_manifest(topic, view):
    root = Path(os.environ.get('QF_NOTEBOOK_ASSET_ROOT', Path(__file__).resolve().parents[1]/'notebook_assets')).resolve()
    records = list(_PORTABLE_ASSETS.get((str(root), str(topic)), {}).values())
    document = {'schemaVersion': 'qf.notebook-portable-assets.v1', 'topic': str(topic),
                'run_id': view.run_id, 'result_sha256': digest(view.directory/'result.json'), 'records': records}
    payload = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode()
    checksum = hashlib.sha256(payload).hexdigest()
    target = root/str(topic)/(checksum+'_asset_manifest.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != payload:
            raise ValueError('DISPLAY_ASSET_MANIFEST_SHA_MISMATCH')
    else:
        target.write_bytes(payload)
    display(Markdown('[随页快照清单与父SHA](../notebook_assets/'+quote(str(topic)+'/'+target.name, safe='/')+')'))


def table(frame, title=None, *, column_widths=None):
    """Display every supplied table row with readable wrapping of identifiers."""
    if title:
        display(Markdown('### '+title))
    if not isinstance(frame, pd.DataFrame):
        frame = pd.DataFrame(frame)
    shown = frame.copy()
    for column in shown:
        shown[column] = shown[column].map(lambda value: html.escape(value) if isinstance(value, str) else value)
        if _CURRENT_TOPIC and str(column) in {'file', 'path', '位置'}:
            for index, value in frame[column].items():
                if isinstance(value, str) and Path(value).is_file():
                    link, _ = portable_asset(value, _CURRENT_TOPIC,
                                             'PROJECT_SOURCE' if Path(value).suffix=='.py' else 'DISPLAYED_ARTIFACT')
                    shown.at[index, column] = '<a href="'+html.escape(link, quote=True)+'">'+html.escape(Path(value).name)+'</a>'
    markup = shown.to_html(index=False, escape=False, max_rows=None, max_cols=None,
                           float_format=lambda x: f'{x:.10g}', border=0)
    if column_widths is not None:
        widths = [float(width) for width in column_widths]
        if (len(widths) != len(shown.columns) or any(not np.isfinite(width) or width <= 0 for width in widths)
                or not np.isclose(sum(widths), 100)):
            raise ValueError('DISPLAY_TABLE_COLUMN_PERCENTAGES_REQUIRED')
        markup = markup.replace('<table ', '<table style="table-layout:fixed;" ', 1)
        opening_end = markup.index('>') + 1
        columns = '<colgroup>' + ''.join(f'<col style="width:{width:g}%;">' for width in widths) + '</colgroup>'
        markup = markup[:opening_end] + columns + markup[opening_end:]
    display(HTML('<div class="qf-table">'+markup+'</div>'))


def page_style():
    display(HTML('''<style>
    .qf-table {font-family:"Times New Roman","SimSun","宋体",serif;font-size:15px;line-height:1.55;margin:12px 0 24px;}
    .qf-table table {width:100%;border-collapse:collapse;text-align:left;table-layout:auto;}
    .qf-table th {border-top:1.5px solid #222;border-bottom:1px solid #555;background:#f3f3f3;padding:8px;text-align:left;}
    .qf-table td {border-bottom:1px solid #ddd;padding:7px 8px;vertical-align:top;overflow-wrap:anywhere;max-width:600px;}
    .qf-run {padding:16px 20px;border-left:3px solid #222;background:#f7f7f7;font-size:15px;margin:15px 0;}
    .jp-OutputArea-output pre {white-space:pre-wrap;overflow-wrap:anywhere;}
    </style>'''))


def show_comparison(views):
    """Show every comparison field in narrow tables with complete identities."""
    frame = compare_runs(views)
    page_style()
    conditions = [column for column in frame if column.startswith('配置.')]
    common = [column for column in conditions if frame[column].nunique(dropna=False) == 1]
    changed = [column for column in conditions if column not in common]
    outcomes = [column for column in frame if column.startswith('结果.')]
    identities = [column for column in frame if column not in conditions+outcomes]
    for title, columns in [('共同条件', common), ('变化参数', changed),
                           ('计算结果', outcomes), ('运行身份与来源', identities)]:
        rows = []
        for column in columns:
            label = column
            if column == '配置.shots':
                label += '（每设置测量次数）'
            elif column in {'结果.shots', '结果.total_shots'}:
                label += '（测量次数合计）'
            rows.append({'指标': label, **{'配置'+chr(65+index): value
                                          for index, value in enumerate(frame[column].tolist())}})
        if rows:
            table(pd.DataFrame(rows), title, column_widths=[25] + [75 / len(frame)] * len(frame))
        else:
            display(Markdown('### '+title+'\n本次比较的该项条件相同。'))
    return frame


def show_overview(view, topic):
    global _CURRENT_TOPIC
    _CURRENT_TOPIC = str(topic)[:2]
    page_style()
    topic = str(topic)[:2]
    display(Markdown('### '+TITLES[topic]))
    display(HTML('<div class="qf-run">运行标识：<b>'+html.escape(view.run_id)+
                 '</b><br>结果SHA256：'+digest(view.directory/'result.json')+'</div>'))
    summary = view.summary
    rows = [{'计算量': LABELS.get(k,k), '字段':k, '本次结果':str(v)}
            for k,v in summary.items() if k in LABELS]
    if rows:
        table(rows, '结果概览')
    table([{'参数':k,'生效值':str(v)} for k,v in view.configuration.items()], '有效运行配置')
    risk = view.documents.get('risk_artifact')
    panel = view.documents.get('input')
    if panel:
        table([{'输入身份':panel['data_epoch'], '资产顺序':'、'.join(panel['asset_order']),
                '起始日期':panel['dates'][0], '终止日期':panel['dates'][-1],
                '观测日期数':len(panel['dates']), '收益单位':panel['return_unit']}], '输入范围')
    if risk:
        display(Markdown(f"本次使用{risk['window_sessions']}期风险窗口，训练区间为"
                         f"{risk['train_dates'][0]}至{risk['train_dates'][-1]}，评价区间为"
                         f"{risk['evaluation_dates'][0]}至{risk['evaluation_dates'][-1]}。"
                         f"最新训练标签成熟于{risk['latest_training_label_available']}。"))


def show_methods(view, topic):
    topic = str(topic)[:2]
    display(Markdown(METHODS[topic]))
    if topic in {'00','01','07'} and 'input' in view.documents:
        panel = view.documents['input']
        frame = pd.DataFrame({'日期':panel['dates'], '标签成熟日期':panel['label_available_dates'],
                              '有效资产标签数':np.asarray(panel['label_valid']).sum(1)})
        table(frame, '决策日期与目标可用性')
        table([{'数组':key, '形状':str(np.asarray(panel[key]).shape), '含义':meaning}
               for key,meaning in [('close_returns','截至收盘的简单收益'),('open_returns','开盘收益'),
                                   ('labels','下一开盘至随后开盘目标'),('label_valid','目标可用掩码')]], '数据轴与单位')
    if topic in {'00','02','07'} and 'risk_artifact' in view.documents:
        risk = view.documents['risk_artifact']
        values = {'资产':risk['asset_order'], '预测收益':risk['mu'],
                  '预测方差':np.diag(risk['Sigma'])}
        if risk.get('predicted_specific_variance') is not None:
            values['特定方差'] = risk['predicted_specific_variance']
        table(pd.DataFrame(values), '收益预测与风险估计')
        if 'feature_trace' in view.arrays:
            trace = view.arrays['feature_trace']
            table([{'中间数组':k,'实际形状':str(v.shape),'最小值':float(v.min()),'最大值':float(v.max())}
                   for k,v in trace.items() if np.issubdtype(v.dtype,np.number)], '特征与编码过程')
        factor = risk['factor_decomposition']
        table([{'组成':k,'形状':str(np.asarray(factor[k]).shape)} for k in ('Lambda','F','d')], '因子协方差组成')
    if topic in {'03','04','07'} and 'bridge_instance' in view.documents:
        states, matrix, encoding = portfolio_tables(view.documents['bridge_instance'])
        table(states, '全部64个状态的目标分解与表示核对')
        table(states.loc[states['可行组合']].sort_values('组合原目标'), '20个可行组合的目标排序')
        table(pd.DataFrame(matrix, columns=[f'x{i}' for i in range(6)]).assign(行=[f'x{i}' for i in range(6)]), 'QUBO系数矩阵')
    for name in ('metrics','candidate_audit','comparisons'):
        if name in view.tables:
            table(view.tables[name], {'metrics':'候选线路与分布质量','candidate_audit':'候选特征、预测与选择','comparisons':'十五项主比较复算'}[name])
    if topic=='04':
        for path in sorted(view.directory.glob('*.qcis')):
            display(Markdown('### '+path.name+'：实际线路\n\n```text\n'+path.read_text()+'\n```'))
    if topic=='05' and 'learning_proxy' in view.documents:
        proxy = view.documents['learning_proxy']
        table(pd.DataFrame({'特征':proxy['features'],'标准化均值':proxy['mean'],
                            '标准化尺度':proxy['scale'],'岭系数':proxy['beta']}), '学习代理参数')
        table([{'正则参数':proxy['alpha'],'截距':proxy['intercept'],'训练候选数':proxy['n_train'],
                '训练样本SHA':proxy['training_sha256']}], '代理拟合记录')


def show_sources(topic):
    global _CURRENT_TOPIC
    _CURRENT_TOPIC = str(topic)[:2]
    for record in source_functions(topic):
        display(Markdown('### '+record['function']))
        table([{k:v for k,v in record.items() if k!='source'}])
        display(Markdown('```python\n'+record['source']+'\n```'))


_FONT_INFO = None


def configure_fonts():
    global _FONT_INFO
    if _FONT_INFO is not None:
        return _FONT_INFO
    import matplotlib
    from matplotlib import font_manager
    choices = {
        'cjk':[os.environ.get('QF_FONT_CJK',''),'/mnt/c/Windows/Fonts/simsun.ttc','C:/Windows/Fonts/simsun.ttc'],
        'western':[os.environ.get('QF_FONT_WESTERN',''),'/mnt/c/Windows/Fonts/times.ttf','C:/Windows/Fonts/times.ttf']}
    found={}
    for kind, names in choices.items():
        path = next((Path(n) for n in names if n and Path(n).is_file()),None)
        if path is None:
            family = 'SimSun' if kind=='cjk' else 'Times New Roman'
            try:
                path=Path(font_manager.findfont(family,fallback_to_default=False))
            except ValueError as exc:
                raise RuntimeError('请通过QF_FONT_CJK与QF_FONT_WESTERN指定宋体及Times New Roman字体文件。') from exc
        font_manager.fontManager.addfont(str(path))
        name = font_manager.FontProperties(fname=str(path)).get_name()
        found[kind]={'path':str(path),'family':name,'sha256':digest(path)}
    matplotlib.rcParams.update({'font.family':[found['western']['family'],found['cjk']['family']],
        'font.size':12,'axes.titlesize':14,'axes.labelsize':12,'xtick.labelsize':12,'ytick.labelsize':12,
        'legend.fontsize':12,'axes.unicode_minus':False,'figure.facecolor':'white','axes.facecolor':'white',
        'savefig.facecolor':'white','svg.fonttype':'path','mathtext.fontset':'stix'})
    _FONT_INFO=found
    return found


def _plot(view, stem, title, draw, *, figsize=(6.1,4.8), selection=None, caption='', nrows=1):
    import matplotlib.pyplot as plt
    fonts=configure_fonts()
    suffix=hashlib.sha256(json.dumps(selection or {},sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:8]
    folder=view.directory/'presentation';folder.mkdir(exist_ok=True)
    name=stem+'_'+suffix
    fig,ax=plt.subplots(nrows=nrows,figsize=figsize,layout='constrained')
    draw(ax)
    if nrows==1:
        ax.set_title(title,pad=16)
    else:
        fig.suptitle(title,fontsize=14)
    png,svg=folder/(name+'.png'),folder/(name+'.svg')
    fig.savefig(png,dpi=600);fig.savefig(svg)
    plt.close(fig)
    record={'title':title,'run_id':view.run_id,'result_sha256':digest(view.directory/'result.json'),
            'selection':selection or {},'png':png.name,'svg':svg.name,'png_sha256':digest(png),
            'svg_sha256':digest(svg),'dpi':600,'width_inches':figsize[0],'fonts':fonts,'caption':caption,
            'drawing_implementation_sha256':digest(Path(__file__)), 'insert_width_cm':15.5,
            'minimum_effective_font_points':12*15.5/2.54/figsize[0]}
    (folder/(name+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    display(Markdown('### '+title))
    display(Image(filename=str(png),width=1000))
    if caption:
        display(Markdown(caption))
    display(Markdown('图源：本次运行 `'+view.run_id+'`；PNG与SVG保存在 `presentation/'+name+'`。'))
    links = [('[原图 PNG]', png), ('[矢量 SVG]', svg), ('[图源记录]', folder/(name+'.json'))]
    display(Markdown(' · '.join(label+'('+portable_asset(path, _CURRENT_TOPIC, 'DISPLAYED_FIGURE')[0]+')'
                               for label, path in links)))


def _heatmap(ax, values, xlabels, ylabels, xlabel='', ylabel=''):
    data=np.asarray(values)
    mesh=ax.imshow(data,cmap='Greys',aspect='auto')
    ax.set_xticks(range(len(xlabels)),xlabels,rotation=35,ha='right')
    ax.set_yticks(range(len(ylabels)),ylabels)
    ax.set(xlabel=xlabel,ylabel=ylabel)
    ax.figure.colorbar(mesh,ax=ax,shrink=.8)


def show_figures(view, topic, selection=None):
    global _CURRENT_TOPIC
    _CURRENT_TOPIC = str(topic)[:2]
    topic=str(topic)[:2];selection=selection or {}
    panel=view.documents.get('input')
    risk=view.documents.get('risk_artifact')
    if panel and topic in {'00','01','07'}:
        assets=panel['asset_order'];selected=selection.get('asset')
        indices=[assets.index(selected)] if selected in assets else list(range(6))
        returns=np.asarray(panel['close_returns'])
        def draw(ax):
            styles=['-','--',':','-.','-','--']
            for i in indices:
                ax.plot(np.arange(len(returns)),returns[:,i],styles[i],color=str(.1+i*.12),label=assets[i],lw=1)
            if selection.get('date') in panel['dates']:
                ax.axvline(panel['dates'].index(selection['date']),color='black',ls='-.',lw=1.5,label=selection['date'])
            ax.set(xlabel='观测序号',ylabel='简单收益率');ax.margins(y=.25);ax.legend(ncol=3)
        _plot(view,'returns','资产收益序列'+('：'+selection['date'] if selection.get('date') in panel['dates'] else ''),draw,selection=selection,
              caption=f"数据区间：{panel['dates'][0]}至{panel['dates'][-1]}；资产顺序沿用输入记录。")
        if topic=='01':
            _plot(view,'label_mask','目标标签可用性',lambda ax:_heatmap(ax,np.asarray(panel['label_valid']).T,
                  [str(i) if i%20==0 else '' for i in range(len(returns))],assets,'观测序号','资产'),selection=selection)
            def distribution(ax):
                ax.boxplot([returns[:,i] for i in range(6)],tick_labels=assets,patch_artist=False,medianprops={'color':'black'})
                ax.set(ylabel='简单收益率');ax.tick_params(axis='x',rotation=30)
            _plot(view,'return_distribution','各资产收益分布',distribution,selection=selection)
    if risk and topic in {'00','02','07'}:
        assets=risk['asset_order']
        _plot(view,'covariance','决策时点协方差',lambda ax:_heatmap(ax,risk['Sigma'],assets,assets),selection=selection,
              caption='单位：单期简单收益率方差；图中行列采用同一资产轴。')
        losses=view.documents.get('training_loss')
        if losses is not None:
            def loss_chart(ax):
                values=np.asarray(losses)
                ax.plot(np.arange(1,len(values)+1),values,color='black')
                ax.set(xlabel='训练轮次',ylabel='训练目标')
            _plot(view,'training_loss','收益学习的训练过程',loss_chart,selection=selection)
        if 'predictions' in view.tables:
            frame=view.tables['predictions'];asset=selection.get('asset',assets[0])
            if asset not in assets:asset=assets[0]
            part=frame.loc[frame.asset==asset]
            def predictions(ax):
                ax.plot(np.arange(len(part)),part.target,'o-',color='black',label='目标收益')
                ax.plot(np.arange(len(part)),part.prediction,'s--',color='.5',label='预测收益')
                ax.set(xlabel='评价日期序号',ylabel='简单收益率');ax.legend()
            _plot(view,'prediction','评价期收益预测：'+asset,predictions,selection=selection)
        if topic=='02' and 'risk_arrays' in view.arrays:
            arr=view.arrays['risk_arrays'];dates=risk['train_dates']+risk['evaluation_dates']
            date=selection.get('date',dates[-1]);index=dates.index(date) if date in dates else len(dates)-1
            labels=[f'Z{i}' for i in range(6)]+[f'Z{i}Z{i+1}' for i in range(5)]
            _plot(view,'readout','量子可测读出：'+dates[index],lambda ax:_heatmap(ax,arr['readout'][index],labels,assets),selection=selection)
            factor=risk['factor_decomposition'];specific=risk.get('predicted_specific_variance')
            if specific is None:specific=factor['d']
            common=np.diag(np.asarray(factor['Lambda'])@np.asarray(factor['F'])@np.asarray(factor['Lambda']).T)
            def decomposition(ax):
                ax.bar(np.arange(6),common,color='.35',label='因子方差')
                ax.bar(np.arange(6),specific,bottom=common,color='white',edgecolor='black',hatch='///',label='特定方差')
                ax.set_xticks(range(6),assets,rotation=30);ax.set(ylabel='方差');ax.legend()
            _plot(view,'risk_decomposition','资产方差的因子与特定项',decomposition,selection=selection)
    if 'bridge_instance' in view.documents and topic in {'03','04','07'}:
        states,matrix,_=portfolio_tables(view.documents['bridge_instance'])
        if topic in {'03','07'}:
            _plot(view,'qubo','QUBO系数',lambda ax:_heatmap(ax,matrix,[f'x{i}' for i in range(6)],[f'x{i}' for i in range(6)]),selection=selection)
            feasible=states.loc[states['可行组合']].sort_values('组合原目标')
            def components(ax):
                for key,style in [('收益贡献','-'),('风险贡献','--'),('费用贡献',':')]:
                    ax.plot(range(20),feasible[key],style,label=key,color='black')
                ax.set(xlabel='可行组合按目标排序',ylabel='目标贡献');ax.legend()
            _plot(view,'objective_components','可行组合的收益、风险与费用',components,selection=selection)
    if 'distribution' in view.tables and topic=='04':
        distribution=view.tables['distribution']
        if 'candidate' in distribution:
            candidate=selection.get('candidate',distribution.candidate.iloc[0])
            distribution=distribution.loc[distribution.candidate==candidate]
        if 'noise' in distribution:distribution=distribution.loc[distribution.noise==distribution.noise.iloc[0]]
        probability=distribution['probability'].to_numpy()
        count_name=next((x for x in ('count','counts') if x in distribution),None)
        def probabilities(ax):
            ax.bar(np.arange(len(probability))-.15,probability,width=.3,color='.3',label='计算概率')
            if count_name:
                counts=distribution[count_name].to_numpy()
                ax.bar(np.arange(len(counts))+.15,counts/counts.sum(),width=.3,color='white',edgecolor='black',label='采样频率')
            ax.set(xlabel='位串对应整数（q5…q0）',ylabel='概率与频率');ax.legend()
        _plot(view,'probabilities','计算概率与采样频率',probabilities,selection=selection)
        if 'objective' in distribution:
            _plot(view,'energy_probability','组合目标与测量概率',lambda ax:(ax.scatter(distribution.objective,probability,c='black',s=24),ax.set(xlabel='组合目标',ylabel='计算概率')),selection=selection)
        if 'feasible' in distribution:
            part=distribution.loc[distribution.feasible.astype(bool)]
            _plot(view,'feasible_probabilities','三资产可行组合的概率',lambda ax:(ax.bar(range(len(part)),part.probability,color='.35'),ax.set(xlabel='可行组合序号',ylabel='计算概率')),selection=selection)
    if 'candidate_audit' in view.tables and topic in {'00','05','07'}:
        frame=view.tables['candidate_audit']
        selected_candidate=selection.get('candidate')
        if selected_candidate in frame.candidate.values:
            score_frame=frame.loc[frame.candidate==selected_candidate]
        else:
            score_frame=frame
        def scores(ax):
            x=np.arange(len(score_frame))
            ax.bar(x-.18,score_frame.observed_target,width=.36,color='.35',label='观测目标差距')
            ax.bar(x+.18,score_frame.prediction,width=.36,color='white',edgecolor='black',hatch='//',label='代理预测')
            ax.set_xticks(x,score_frame.candidate,rotation=25);ax.set(ylabel='目标差距');ax.margins(y=.3);ax.legend()
        _plot(view,'candidate_scores','候选质量与代理预测'+('：'+selected_candidate if selected_candidate else ''),scores,selection=selection)
        if topic=='05':
            def fit(ax):
                for name,part in frame.groupby('split'):
                    ax.scatter(part.observed_target,part.prediction,label=name,marker='o' if name=='train' else 's',s=65,facecolors='white',edgecolors='black')
                low=min(frame.observed_target.min(),frame.prediction.min());high=max(frame.observed_target.max(),frame.prediction.max())
                ax.plot([low,high],[low,high],'--',color='.5');ax.set(xlabel='观测目标差距',ylabel='代理预测');ax.legend()
            _plot(view,'proxy_fit','训练与留出候选的代理预测',fit,selection=selection)
            proxy=view.documents['learning_proxy']
            _plot(view,'proxy_coefficients','学习代理的标准化系数',lambda ax:(ax.barh(range(len(proxy['beta'])),proxy['beta'],color='.35'),ax.set_yticks(range(len(proxy['beta'])),[f'特征{i+1}' for i in range(len(proxy['beta']))]),ax.set(xlabel='岭回归系数')),selection=selection,
                  caption='特征序号与上方学习代理参数表的行顺序一致。')
        trace=view.documents.get('optimization_trace')
        if trace and topic in {'05','07'}:
            def optimization(ax):
                for i,row in enumerate(trace['candidates']):
                    if selection.get('candidate') and row['candidate']!=selection['candidate']:continue
                    if row['evaluations']:
                        ax.plot([r['evaluation'] for r in row['evaluations']], [r['objective'] for r in row['evaluations']],
                                ['-','--',':','-.','-','--'][i%6],color=str(.1+i*.12),label=row['candidate'])
                ax.set(xlabel='实际求值序号',ylabel='期望目标');ax.margins(y=.2);ax.legend(ncol=2)
            _plot(view,'optimization','候选线路的实际求值轨迹',optimization,selection=selection)
    if topic=='06' and 'comparisons' in view.tables:
        frame=view.tables['comparisons']
        def intervals(axes):
            groups=[(('E02','E03'),'学习任务：MAE差值'),
                    (('E04','E07'),'持仓路径：CVaR95差值'),
                    (('E05','E06'),'量子求解与选线：目标差值')]
            for ax,(experiments,label) in zip(axes,groups):
                part=frame.loc[frame.experiment.isin(experiments)]
                for position,(_,row) in enumerate(part.iterrows()):
                    ci=row['ci95'];ci=json.loads(ci) if isinstance(ci,str) else ci
                    ax.plot(ci,[position,position],color='black')
                    ax.scatter([row.effect],[position],color='black',s=25)
                ax.axvline(0,color='.6',ls='--')
                ax.set_yticks(range(len(part)),[f'{row.experiment} · 比较{i+1:02d}' for i,row in part.iterrows()])
                ax.invert_yaxis()
                ax.set_title(label,pad=12,fontsize=12)
                ax.set(xlabel='原指标效应与95%置信区间')
                ax.ticklabel_format(axis='x',style='sci',scilimits=(-3,3))
                ax.margins(y=.16)
        _plot(view,'effects','十五项主比较的效应与区间',intervals,figsize=(6.1,10.5),selection=selection,nrows=3,
              caption='三个面板分别使用MAE、CVaR95和组合目标的原指标差值及独立横轴。比较序号与上方复算表行顺序一致，比较方向、统计单位和条件沿用该行原实验定义。')
        _plot(view,'adjusted_p','实验组内Holm校正p值',lambda ax:(ax.barh(range(len(frame)),frame.p_holm,color='.35'),ax.set_yticks(range(len(frame)),[f'比较{i+1:02d}' for i in range(len(frame))]),ax.set(xlabel='校正p值',xlim=(0,1))),figsize=(6.1,7.5),selection=selection)
        _plot(view,'statistical_error','统计复算的最大数值误差',lambda ax:(ax.bar(range(len(frame)),frame.maximum_numeric_error,color='.35'),ax.set(xlabel='主比较序号',ylabel='最大绝对误差')),selection=selection)


def show_audit(view):
    checks=audit_run(view)
    table(checks,'计算关系与文件身份核对')
    if len(checks):
        display(Markdown(f"本次完成{len(checks)}项核对，通过{int((checks['结果']=='通过').sum())}项。每项列出实际误差和所用容差。"))
    return checks


def show_provenance(view):
    global _CURRENT_TOPIC
    _CURRENT_TOPIC = view.result.get('notebook', {}).get('topic', _CURRENT_TOPIC)
    keys=('runId','run_id','experimentId','dataEpoch','data_epoch','executionMode','sourceCommit','workspaceCommit')
    table([{'记录字段':k,'实际值':str(view.result[k])} for k in keys if k in view.result], '运行身份')
    table(artifact_inventory(view),'本次工件完整清单')
    parents=view.result.get('parents',{})
    if isinstance(parents,dict):
        table([{'来源':k,'记录':json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else str(v)} for k,v in parents.items()], '输入来源')
    elif isinstance(parents,list):
        table(pd.json_normalize(parents),'输入来源')
    environment=view.result.get('environment',{})
    if environment:
        table([{'环境项':k,'实际值':str(v)} for k,v in environment.items()], '计算环境')
    implementation=view.result.get('implementation',{})
    if implementation:
        table([{'实现文件':k,'SHA256':v} for k,v in implementation.items()], '实现文件身份')
    _asset_manifest(_CURRENT_TOPIC, view)


def render_run(view,topic,selection=None):
    """Render the current interactive result with its configuration and audit trail."""
    show_overview(view,topic)
    show_figures(view,topic,selection)
    show_audit(view)
    show_provenance(view)
