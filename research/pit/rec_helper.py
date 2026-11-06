import pandas as pd, datetime as dt, json, sys
CLS='research/pit/classification'
def record(rid, code, items, common, interp='기간 내 사업보고서 전부 확인, 각 보고서 자체 수치(보간 없음)'):
    """items: list of (rcept_dt_prefix YYYYMM or rcept_no, verdict, part, evidence)"""
    c=pd.read_csv(f'{CLS}/classification.csv',dtype=str); f=pd.read_csv(f'{CLS}/listing/annual_master.csv',dtype=str)  # 09:36 이후: 첨부추가 포함 최선 버전 1건/연도
    a=f[(f.code==code)&(f.rcept_dt>='20150101')&(f.fy.astype(int)>=2014)].sort_values('rcept_dt')
    now=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime('%Y-%m-%d %H:%M KST')
    rows=[]; used=set()
    for key,v,p,e in items:
        m=a[(a.rcept_no==key)|(a.rcept_dt.str.startswith(key))]
        assert len(m)==1,(code,key,len(m)); x=m.iloc[0]; used.add(x.rcept_no)
        rows.append({'review_id':rid,'reviewed_at':now,'code':code,'corp_code':x.corp_code,'name':x.corp_name,'effective_from':x.rcept_dt,
          'basis_rcept_no':x.rcept_no,'basis_report':x.report_nm,'verdict':v,'part':p,'evidence':(common+' | '+e) if e else common,
          'interpolation':interp,'returns_viewed':'no','hold_class':''})
    miss=set(a.rcept_no)-used; assert not miss,(code,'누락 보고서',sorted(a[a.rcept_no.isin(miss)].rcept_dt))
    c=pd.concat([c,pd.DataFrame(rows)],ignore_index=True); c.to_csv(f'{CLS}/classification.csv',index=False,encoding='utf-8-sig')
    print(code, a.corp_name.iloc[-1], len(rows),'건', '/'.join(dict.fromkeys(r['verdict']+('·'+r['part'] if r['part'] else '') for r in rows)))
def same(code, v, p, pairs):  # pairs: [(YYYYMM, evidence)]
    return [(k,v,p,e) for k,e in pairs]

def add(rid, code, rcept_no, v, p, e, interp='복구 보고서 단건 재판정'):
    c=pd.read_csv(f'{CLS}/classification.csv',dtype=str); f=pd.read_csv(f'{CLS}/listing/annual_master.csv',dtype=str)
    x=f[f.rcept_no==rcept_no].iloc[0]
    now=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime('%Y-%m-%d %H:%M KST')
    row={'review_id':rid,'reviewed_at':now,'code':code,'corp_code':x.corp_code,'name':x.corp_name,'effective_from':x.rcept_dt,
         'basis_rcept_no':rcept_no,'basis_report':x.report_nm,'verdict':v,'part':p,'evidence':e,'interpolation':interp,'returns_viewed':'no','hold_class':''}
    pd.concat([c,pd.DataFrame([row])],ignore_index=True).to_csv(f'{CLS}/classification.csv',index=False,encoding='utf-8-sig')
    print(code,x.corp_name,x.report_nm,v,p)
