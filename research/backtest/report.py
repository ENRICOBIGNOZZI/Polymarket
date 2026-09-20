"""Seven compact plots/table and an honest, self-contained economic report."""
from datetime import datetime,timezone
import json
from pathlib import Path


def fmt(x):return 'N/D' if x is None else f'{x:.4f}' if isinstance(x,float) else str(x)


def render(root, frozen, result):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    root=Path(root);m=result['final_test'];missing=m.get('status')=='MODEL_FORECASTS_UNAVAILABLE'
    def save(name,title,draw):
        fig,ax=plt.subplots(figsize=(8,4));draw(ax);ax.set_title(title);ax.grid(alpha=.2);fig.tight_layout()
        fig.savefig(root/(name+'.png'),dpi=150);fig.savefig(root/(name+'.svg'));plt.close(fig)
    def unavailable(ax):ax.text(.5,.5,'PnL non valutabile: previsioni del modello assenti',ha='center',va='center',transform=ax.transAxes);ax.set_xticks([]);ax.set_yticks([])
    def cumulative(ax):
        c=m['cumulative_pnl']
        if missing or not c:return unavailable(ax)
        ax.plot([datetime.fromtimestamp(x/1000,timezone.utc) for x,y in c],[y for x,y in c]);ax.set_ylabel('PAPER PnL USD');ax.set_xlabel('UTC, settlement realizzato')
    save('01-cumulative-pnl','TEST: PnL cumulativo',cumulative)
    def latency(ax):
        points=[(int(k),v['net_pnl']) for k,v in result['latency'].items() if v['net_pnl'] is not None]
        if not points:return unavailable(ax)
        ax.plot(*zip(*points),marker='o');ax.set_xlabel('Latenza totale ipotizzata (ms)');ax.set_ylabel('PAPER PnL USD')
    save('02-pnl-latency','TEST: PnL vs latenza',latency)
    for name,title,key in [('03-pnl-asset','PnL per crypto','by_asset'),('04-pnl-horizon','PnL per orizzonte','by_horizon'),('05-pnl-tte','PnL per TTE','by_tte')]:
        def draw(ax,key=key):
            if missing:return unavailable(ax)
            vals=result[key];ax.bar(list(vals),[v['observed_net_pnl'] or 0 for v in vals.values()]);ax.set_ylabel('PnL osservato USD')
        save(name,'TEST: '+title,draw)
    def lead(ax):
        available=False
        for asset in sorted({v['asset'] for v in result['lead_lag']}):
            points=[(v['horizon_ms'],v['mean_directional_pm_move']*100) for v in result['lead_lag'] if v['asset']==asset and v['mean_directional_pm_move'] is not None]
            if points:ax.plot(*zip(*points),marker='o',label=asset);available=True
        if available:ax.legend();ax.axhline(0,color='black',lw=.5)
        else:ax.text(.5,.5,'Copertura post-segnale insufficiente',ha='center',transform=ax.transAxes)
        ax.set_xlabel('Orizzonte dopo decisione (ms)');ax.set_ylabel('Movimento PM nella direzione del segnale (punti %)')
    save('06-external-pm-repricing','TEST: segnale crypto → repricing PM',lead)
    selected=frozen['selected'];configs=frozen['validation_configurations']
    baseline=next(r for r in configs if r['baseline']);shown=[next(r for r in configs if r['parameters']==selected)]
    shown += [r for r in configs if r not in shown][:3]
    if baseline not in shown:shown.append(baseline)
    header=['EV','TTE s','Età ms','Cap','Quote','Trade','PnL USD']
    body=[]
    for r in shown:
        p=r['parameters'];v=r['metrics'];body.append([fmt(p['edge']),f"{p['tte_min']}–{p['tte_max']}",p['signal_age_ms'],p['entry_cap'],p['shares'],v['trades'],fmt(v['net_pnl'])])
    fig,ax=plt.subplots(figsize=(10,2.5));ax.axis('off');ax.table(cellText=body,colLabels=header,loc='center');ax.set_title('VALIDATION: configurazione congelata, alternative e baseline');fig.tight_layout();fig.savefig(root/'07-parameter-comparison.png',dpi=150);plt.close(fig)
    v=next(r['metrics'] for r in configs if r['parameters']==selected)
    lines=['# Backtest PAPER: dati recenti', '',f"Intervallo UTC: {datetime.fromtimestamp(result['data_coverage']['start_ms']/1000,timezone.utc).isoformat()} — {datetime.fromtimestamp(result['data_coverage']['end_ms']/1000,timezone.utc).isoformat()}.",
        '',f"**{('RISULTATO ECONOMICO NON VALUTABILE: previsioni del modello assenti' if missing else 'FINAL HISTORICAL TEST RESULT')}**",'',
        'Nessun training, modifica di modello o invio ordini. `paper_only=true`, `authenticated_execution=false`, `real_order_submission=false`.',
        '',f"Stato selezione: `{frozen['selection_status']}`. Una baseline congelata per insufficienza di dati non è una configurazione vincente.",
        '',f"Modello: {frozen['model_description']} SHA: {frozen['model_hash'] or 'assente'}.",
        '',f"Parametri congelati: EV {selected['edge']}, TTE {selected['tte_min']}–{selected['tte_max']} s, età massima {selected['signal_age_ms']} ms, cap {selected['entry_cap']}, {selected['shares']} quote.",
        '', '## VALIDATION RESULT','',f"{len(configs)} configurazioni prespecificate; trade {v['trades']}, fill {v['fills']}, PnL {fmt(v['net_pnl'])}. Minimo 5 mercati con fill e copertura contabile completa per selezionare.",
        '', '| '+' | '.join(header)+' |','|'+'|'.join(['---']*len(header))+'|']
    lines += ['| '+' | '.join(map(str,row))+' |' for row in body]
    lines += ['', '## FINAL HISTORICAL TEST RESULT','',f"Opportunità {m['opportunities']}; senza previsione {m['opportunities_without_forecast']}; trade {m['trades']}; fill {m['fills']}; fill rate {fmt(m['fill_rate'])}; PnL {fmt(m['net_pnl'])}; PnL/trade {fmt(m['pnl_per_trade'])}; fee {fmt(m['fees'])}; turnover {fmt(m['turnover'])}; drawdown {fmt(m['max_drawdown'])}.",
        '', 'Lo zero nei conteggi di ordini non dimostra PnL economico nullo quando manca il modello. Il drawdown, se disponibile, è sul PnL realizzato a settlement.',
        '', '| Latenza totale ms | Trade | Fill | PnL USD | Deterioramento medio |','|---|---|---|---|---|']
    lines += [f"| {k} | {x['trades']} | {x['fills']} | {fmt(x['net_pnl'])} | {fmt(x['average_execution_deterioration'])} |" for k,x in result['latency'].items()]
    lines += ['', 'Latenze prespecificate, nessuna scelta sulla base del TEST. La latenza London reale non è stata misurata. Libro osservato DOPO la latenza, tolleranza massima 50 ms aggiuntivi; solo quantità L1 visibile; fill parziali; fee storiche. Nessun prezzo precedente riutilizzato. Un tentativo per mercato, cap 3,75 USD e 20 quote; riserva complessiva 1.000 USD senza riciclo.',
        '', '## Lead-lag diagnostico','', '| Crypto | Orizzonte ms | Campioni | Mercati | Movimento PM direzionale |','|---|---|---|---|---|']
    lines += [f"| {x['asset']} | {x['horizon_ms']} | {x['samples']} | {x['markets']} | {fmt(x['mean_directional_pm_move'])} |" for x in result['lead_lag']]
    lines += ['', 'Media condizionata alla copertura disponibile, non un rendimento negoziabile. Segnali nativi validi e confermati; un segnale per mercato/secondo. I token DOWN sono già orientati nella direzione prevista. L’orizzonte è dalla decisione, non dal timestamp dell’exchange esterno.', '', '## Risposte economiche','']
    if missing:
        answers=['Profitto storico dopo esecuzione: non determinabile senza le previsioni.', 'Profitto sul TEST intatto: non valutabile; nessuna ottimizzazione sul TEST.',
            f"Trade che generano il risultato: {m['trades']}; non esiste un PnL stimato del modello.", 'Concentrazione del PnL: non determinabile.',
            'Crypto migliore per PnL: non determinabile; tutte e sei sono incluse nei dati.', 'Orizzonte migliore per PnL: non determinabile.',
            'TTE migliore: non selezionabile; baseline conservata.', 'Età del segnale migliore: non selezionabile.', 'EV minimo migliore: non selezionabile.',
            'Sensibilità economica alla latenza: non determinabile.', 'Latenza di scomparsa dell’edge: non determinabile.',
            'Lead-lag: vedere misure per crypto; una media positiva non prova redditività dopo spread e fee.',
            'Forward PAPER: continuare la raccolta; questi dati da soli non giustificano l’attivazione di un modello redditizio. Servono previsioni da un artefatto già validato.']
    else:
        answers=[f"PnL TEST: {fmt(m['net_pnl'])}, fill: {m['fills']}. Confrontare validation e TEST nei dati allegati; una sola breve finestra non dimostra stabilità."]
    lines += [f'{i}. {a}' for i,a in enumerate(answers,1)]
    lines += ['', '## Grafici','']+[f'![{name}]({name}.png)' for name in ('01-cumulative-pnl','02-pnl-latency','03-pnl-asset','04-pnl-horizon','05-pnl-tte','06-external-pm-repricing','07-parameter-comparison')]
    (root/'REPORT.md').write_text('\n'.join(lines)+'\n')
