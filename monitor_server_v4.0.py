"""
================================================================
 MT5 Monitor Server v3.2  (พอร์ต 8000)
================================================================
รวมทุกอย่างไว้ในเซิร์ฟเวอร์เดียว เสิร์ฟ dashboard ให้มือถือ:
  /ingest    รับข้อมูลจาก EA reporter
  /accounts  รายการบัญชีทั้งหมด
  /history   ประวัติ equity รวม (กราฟ)
  /gold      ราคาทอง + % รายวันจริง (จำราคาเปิดวัน ไม่รีเซ็ตตอนรีเฟรช)   [ใหม่ v3.2]
  /gauges    มาตรวัดแรงซื้อ/ขาย 4 ช่วง 1H/30m/15m/5m                      [ใหม่ v3.2]
  /news      ข่าวกล่องแดงที่กระทบทอง + เวลา                                 [ใหม่ v3.2]

ติดตั้ง:  ดับเบิลคลิก install.bat  (ต้องมี requests ด้วย)
รัน    :  ดับเบิลคลิก start.bat
----------------------------------------------------------------
ข่าว (ไม่บังคับ): ตั้ง NEWS_API_KEY ใน start.bat ถ้าต้องการข่าวจริง
   สมัคร key ฟรีที่ https://www.jblanked.com/news/api/
   ไม่ใส่ key = ตารางข่าวจะว่าง (ส่วนอื่นทำงานปกติ)
================================================================
"""
import os, time, json, threading, datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
try:
    import requests
except Exception:
    requests = None

# ===== ตั้งค่า =====
TOKEN       = os.environ.get("MONITOR_TOKEN", "0000")
NEWS_KEY    = os.environ.get("NEWS_API_KEY", "")
STALE_SEC   = 20
HIST_SEC    = 60
HIST_MAX    = 60000
HERE        = os.path.dirname(os.path.abspath(__file__))
HIST_FILE   = os.path.join(HERE, "history.json")
GOLD_FILE   = os.path.join(HERE, "gold_state.json")
DD_FILE     = os.path.join(HERE, "dd_state.json")
PL_FILE     = os.path.join(HERE, "pl_extremes.json")

app = FastAPI(title="MT5 Monitor v4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ACCOUNTS: dict = {}
HISTORY:  list = []
_last_hist = [0.0]

PRICES = []          # [(ts,price)] ~3 ชม. สำหรับมาตรวัด
GOLD   = {}
GAUGES = {}
NEWS   = {"items": [], "updated": 0, "note": ""}

# ---------- history ----------
try:
    with open(HIST_FILE, encoding="utf-8") as f:
        HISTORY = json.load(f)
    print(f"โหลดประวัติ {len(HISTORY)} จุด")
except Exception:
    HISTORY = []

def save_history():
    try:
        with open(HIST_FILE, "w", encoding="utf-8") as f:
            json.dump(HISTORY[-HIST_MAX:], f)
    except Exception as e:
        print("save history err:", e)

def record_point():
    now = time.time()
    if now - _last_hist[0] < HIST_SEC:
        return
    _last_hist[0] = now
    live = [s for s in ACCOUNTS.values() if now - s.get("_rx", 0) <= STALE_SEC]
    if not live:
        return
    eq  = sum(s.get("equity",  0) or 0 for s in live)
    bal = sum(s.get("balance", 0) or 0 for s in live)
    pl  = sum(s.get("profit",  0) or 0 for s in live)
    HISTORY.append({"t": int(now), "equity": round(eq,2), "balance": round(bal,2), "profit": round(pl,2)})
    if len(HISTORY) > HIST_MAX:
        del HISTORY[:len(HISTORY)-HIST_MAX]
    save_history()

# ---------- DD รายวัน (จุดสูงสุด equity ต่อบัญชี ต่อวัน) ----------
DD_PEAKS = {}          # {login_str: {"day":"YYYY-MM-DD","peak":float}}
try:
    with open(DD_FILE, encoding="utf-8") as f:
        DD_PEAKS = json.load(f)
except Exception:
    DD_PEAKS = {}

_last_dd_save = [0.0]
def save_dd_peaks():
    try:
        with open(DD_FILE, "w", encoding="utf-8") as f:
            json.dump(DD_PEAKS, f)
    except Exception as e:
        print("save dd err:", e)

def update_dd_peak(login, equity):
    if equity is None: return
    key = str(login)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    rec = DD_PEAKS.get(key)
    if not rec or rec.get("day") != today:
        # วันใหม่: เริ่มนับใหม่ทั้งจุดสูงสุด equity และ DD สูงสุดที่เคยลง
        rec = {"day": today, "peak": equity, "max_dd_amt": 0.0, "max_dd_pct": 0.0}
    else:
        rec["peak"] = max(rec.get("peak", equity), equity)
    # DD ปัจจุบัน (ระยะห่างจาก peak ตอนนี้)
    peak = rec["peak"]
    cur_amt = max(0.0, peak - equity)
    cur_pct = (cur_amt/peak*100) if peak else 0.0
    # ค่า "สูงสุดที่เคยลงไปวันนี้" — เก็บไว้แบบไม่ลดลง แม้บัญชีจะฟื้นภายหลัง
    rec["max_dd_amt"] = max(rec.get("max_dd_amt", 0.0), cur_amt)
    rec["max_dd_pct"] = max(rec.get("max_dd_pct", 0.0), cur_pct)
    DD_PEAKS[key] = rec
    now = time.time()
    if now - _last_dd_save[0] > 15:   # เขียนไฟล์ไม่ถี่เกินไป
        _last_dd_save[0] = now
        save_dd_peaks()

def dd_for(login, equity):
    """
    DD รายวัน = (จุดสูงสุด equity วันนี้ - equity ตอนนี้) / จุดสูงสุด * 100  (ค่าสด เปลี่ยนตาม P/L)
    Max DD วันนี้ = ค่า DD ที่ลึกที่สุดที่เคยเกิดวันนี้ (จำไว้ ไม่ลดลงแม้บัญชีจะฟื้น จนกว่าจะขึ้นวันใหม่)
    """
    rec = DD_PEAKS.get(str(login))
    if not rec or equity is None:
        return {"dd_pct":0.0,"dd_amt":0.0,"dd_peak":equity,"dd_max_pct":0.0,"dd_max_amt":0.0}
    peak = rec.get("peak", equity)
    if not peak or peak <= 0:
        return {"dd_pct":0.0,"dd_amt":0.0,"dd_peak":peak,"dd_max_pct":0.0,"dd_max_amt":0.0}
    amt = max(0.0, peak - equity)
    pct = (amt / peak * 100) if peak else 0.0
    return {"dd_pct": round(pct, 2), "dd_amt": round(amt, 2), "dd_peak": round(peak, 2),
            "dd_max_pct": round(rec.get("max_dd_pct",0.0), 2),
            "dd_max_amt": round(rec.get("max_dd_amt",0.0), 2)}

# ---------- กำไรสูงสุด / ขาดทุนสูงสุดที่เคยเกิดวันนี้ (จาก P/L ลอยตัว) ----------
PL_EXTREMES = {}      # {login_str: {"day":"YYYY-MM-DD","max_profit":float,"min_profit":float}}
try:
    with open(PL_FILE, encoding="utf-8") as f:
        PL_EXTREMES = json.load(f)
except Exception:
    PL_EXTREMES = {}

_last_pl_save = [0.0]
def save_pl_extremes():
    try:
        with open(PL_FILE, "w", encoding="utf-8") as f:
            json.dump(PL_EXTREMES, f)
    except Exception as e:
        print("save pl err:", e)

def update_pl_extremes(login, profit):
    """profit = P/L ลอยตัวปัจจุบัน — จำค่าสูงสุด(กำไรมากสุด)และต่ำสุด(ขาดทุนมากสุด)ที่เคยเห็นวันนี้"""
    if profit is None: return
    key = str(login)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    rec = PL_EXTREMES.get(key)
    if not rec or rec.get("day") != today:
        rec = {"day": today, "max_profit": profit, "min_profit": profit}
    else:
        rec["max_profit"] = max(rec.get("max_profit", profit), profit)
        rec["min_profit"] = min(rec.get("min_profit", profit), profit)
    PL_EXTREMES[key] = rec
    now = time.time()
    if now - _last_pl_save[0] > 15:
        _last_pl_save[0] = now
        save_pl_extremes()

def pl_extremes_for(login):
    rec = PL_EXTREMES.get(str(login))
    if not rec:
        return {"max_profit": 0.0, "min_profit": 0.0}
    return {"max_profit": round(rec.get("max_profit", 0.0), 2),
            "min_profit": round(rec.get("min_profit", 0.0), 2)}

# ---------- ช่วง P/L รวมทั้งพอร์ตวันนี้ (สะสมแบบไม่ลดลง) ----------
def update_portfolio_extremes():
    """รวม P/L ลอยตัวของทุกบัญชีที่ยังสด แล้วจำค่าสูงสุด/ต่ำสุดของวันนี้"""
    now = time.time()
    live = [s for s in ACCOUNTS.values() if now - s.get("_rx", 0) <= STALE_SEC]
    if not live: return
    total = sum(s.get("profit", 0) or 0 for s in live)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    rec = PL_EXTREMES.get("__PORTFOLIO__")
    if not rec or rec.get("day") != today:
        rec = {"day": today, "max_profit": total, "min_profit": total}
    else:
        rec["max_profit"] = max(rec.get("max_profit", total), total)
        rec["min_profit"] = min(rec.get("min_profit", total), total)
    PL_EXTREMES["__PORTFOLIO__"] = rec

def portfolio_extremes():
    rec = PL_EXTREMES.get("__PORTFOLIO__")
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if not rec or rec.get("day") != today:
        return {"max_profit": 0.0, "min_profit": 0.0}
    return {"max_profit": round(rec.get("max_profit", 0.0), 2),
            "min_profit": round(rec.get("min_profit", 0.0), 2)}

# ---------- gold state (ราคาเปิดวัน) ----------
def load_gold_state():
    try:
        with open(GOLD_FILE, encoding="utf-8") as f: return json.load(f)
    except Exception: return {}
def save_gold_state(s):
    try:
        with open(GOLD_FILE, "w", encoding="utf-8") as f: json.dump(s, f)
    except Exception: pass

GOLD_STATUS = {"ok": True, "fail_count": 0, "last_error": "", "last_try_ts": 0}

def poll_gold():
    if requests is None: return
    GOLD_STATUS["last_try_ts"] = time.time()
    try:
        r = requests.get("https://api.gold-api.com/price/XAU", timeout=8,
                          headers={"User-Agent": "mt5monitor/3.6"})
        r.raise_for_status()
        p = float(r.json().get("price"))
        if not p or p <= 0:
            raise ValueError(f"ราคาที่ได้ไม่ถูกต้อง: {p}")
    except Exception as e:
        GOLD_STATUS["ok"] = False
        GOLD_STATUS["fail_count"] += 1
        GOLD_STATUS["last_error"] = str(e)
        print(f"[gold] ดึงราคาทองล้มเหลว ({GOLD_STATUS['fail_count']} ครั้งติด): {e}")
        return
    GOLD_STATUS["ok"] = True
    GOLD_STATUS["fail_count"] = 0
    GOLD_STATUS["last_error"] = ""

    now = time.time()
    PRICES.append((now, p))
    cut = now - 3*3600
    while PRICES and PRICES[0][0] < cut: PRICES.pop(0)

    st = load_gold_state()
    d  = datetime.datetime.now().strftime("%Y-%m-%d")
    if st.get("day") != d:
        st = {"day": d, "open": p}
        save_gold_state(st)
    op  = st.get("open", p)
    chg = p - op
    pct = (chg/op*100) if op else 0
    GOLD.clear()
    GOLD.update({"price":round(p,2),"day_open":round(op,2),
                 "change":round(chg,2),"change_pct":round(pct,3),"ts":int(now),
                 "ok":True})
    compute_gauges()

# ---------- gauges ----------
def _ema(v,n):
    if not v: return None
    k=2.0/(n+1); e=v[0]
    for x in v: e=x*k+e*(1-k)
    return e
def _rsi(v,n=14):
    if len(v)<=n: return None
    g=l=0.0
    for i in range(1,n+1):
        dd=v[i]-v[i-1]; g+=max(dd,0); l+=max(-dd,0)
    ag=g/n; al=l/n
    for i in range(n+1,len(v)):
        dd=v[i]-v[i-1]; ag=(ag*(n-1)+max(dd,0))/n; al=(al*(n-1)+max(-dd,0))/n
    if al==0: return 100.0
    return 100-100/(1+ag/al)

def gauge_for(sec):
    now=time.time()
    seg=[p for (t,p) in PRICES if t>=now-sec]
    if len(seg)<10:
        return {"label":"รอข้อมูล","score":0,"buy":0,"sell":0,"neutral":0,"ready":False}
    buy=sell=neu=0
    r=_rsi(seg,14)
    if r is not None:
        if r<30:buy+=1
        elif r>70:sell+=1
        else:neu+=1
    ef=_ema(seg,9); es=_ema(seg,21)
    if ef and es:
        if ef>es:buy+=1
        elif ef<es:sell+=1
        else:neu+=1
    mom=seg[-1]-seg[0]
    if mom>0:buy+=1
    elif mom<0:sell+=1
    else:neu+=1
    el=_ema(seg,50) or es
    if el:
        if seg[-1]>el:buy+=1
        elif seg[-1]<el:sell+=1
        else:neu+=1
    score=buy-sell
    lbl=("มีแรงซื้อรุนแรง" if score>=2 else "มีแรงซื้อ" if score==1 else
         "เป็นกลาง" if score==0 else "มีแรงขาย" if score==-1 else "มีแรงขายรุนแรง")
    return {"label":lbl,"score":score,"buy":buy,"sell":sell,"neutral":neu,"ready":True}

def compute_gauges():
    GAUGES.clear()
    GAUGES.update({"1H":gauge_for(3600),"30m":gauge_for(1800),
                   "15m":gauge_for(900),"5m":gauge_for(300),"ts":int(time.time())})

# ---------- news ----------
GOLD_CCY={"USD","XAU","ALL"}
def poll_news():
    if requests is None or not NEWS_KEY:
        NEWS["items"]=[]; NEWS["note"]="ยังไม่ใส่ NEWS_API_KEY (ข่าวปิดอยู่)"; NEWS["updated"]=int(time.time())
        return
    try:
        r=requests.get("https://www.jblanked.com/news/api/forex-factory/calendar/today/",
                       headers={"Authorization":f"Api-Key {NEWS_KEY}","Content-Type":"application/json"},
                       timeout=12)
        arr=r.json() if r.status_code==200 else []
    except Exception as e:
        NEWS["note"]=f"ดึงข่าวไม่ได้: {e}"; return
    items=[]
    for ev in (arr or []):
        if "high" not in str(ev.get("Impact","")).lower(): continue
        ccy=str(ev.get("Currency","")).upper()
        if ccy not in GOLD_CCY: continue
        items.append({"time":ev.get("Date",""),"currency":ccy,"name":ev.get("Name",""),
                      "forecast":ev.get("Forecast",""),"previous":ev.get("Previous",""),
                      "actual":ev.get("Actual","")})
    NEWS["items"]=items; NEWS["updated"]=int(time.time()); NEWS["note"]=""

# ===== Endpoints =====
@app.post("/ingest")
async def ingest(req: Request):
    try:
        d = await req.json()
    except Exception:
        raise HTTPException(400, "JSON ไม่ถูกต้อง")
    if TOKEN and d.get("token") != TOKEN:
        raise HTTPException(401, "token ไม่ถูกต้อง")
    login = d.get("login")
    if login is None:
        raise HTTPException(400, "ไม่มี login")
    d.pop("token", None)
    d["_rx"] = time.time()
    ACCOUNTS[str(login)] = d
    update_dd_peak(login, d.get("equity", d.get("balance")))
    update_pl_extremes(login, d.get("profit"))
    update_portfolio_extremes()
    record_point()
    return {"ok": True}

@app.get("/accounts")
def accounts():
    now = time.time(); out=[]
    for s in ACCOUNTS.values():
        a=dict(s); age=now-a.pop("_rx",now)
        a["age"]=round(age,1); a["stale"]=age>STALE_SEC
        dd=dd_for(a.get("login"), a.get("equity", a.get("balance")))
        a["dd_today_pct"]=dd["dd_pct"]; a["dd_today_amt"]=dd["dd_amt"]; a["dd_peak"]=dd["dd_peak"]
        a["dd_max_pct"]=dd["dd_max_pct"]; a["dd_max_amt"]=dd["dd_max_amt"]
        pl=pl_extremes_for(a.get("login"))
        a["pl_max_today"]=pl["max_profit"]; a["pl_min_today"]=pl["min_profit"]
        out.append(a)
    out.sort(key=lambda x: x.get("login",0))
    return out

@app.get("/history")
def history(range: str = "1D"):
    spans={"1H":3600,"4H":14400,"1D":86400,"1W":604800,"1M":2592000}
    cutoff=time.time()-spans.get(range.upper(),86400)
    pts=[p for p in HISTORY if p["t"]>=cutoff]
    if len(pts)>300:
        step=max(1,len(pts)//300); pts=pts[::step]
    return {"range":range.upper(),"points":pts}

@app.get("/gold")
def gold():
    g = dict(GOLD) if GOLD else {"price":None}
    age = time.time() - GOLD.get("ts", 0) if GOLD.get("ts") else None
    g["age_sec"] = round(age,1) if age is not None else None
    g["stale"] = (age is None) or (age > 90)
    g["fail_count"] = GOLD_STATUS.get("fail_count", 0)
    g["last_error"] = GOLD_STATUS.get("last_error", "")
    return g

@app.get("/gauges")
def gauges(): return GAUGES or {}

@app.get("/portfolio")
def portfolio():
    """ช่วง P/L รวมทั้งพอร์ตวันนี้ (สูงสุด/ต่ำสุดที่เคยแตะ)"""
    return portfolio_extremes()
@app.get("/news")
def news(): return NEWS

@app.get("/", response_class=HTMLResponse)
def index():
    for name in ("monitor_v4.0.html","monitor_v3.9.html","monitor_v3.8.html","monitor_v3.7.html","monitor_v3.6.html","monitor_v3.5.html","monitor_v3.4.html","monitor_v3.3.html","monitor_v3.2.html","monitor_v3.html"):
        p=os.path.join(HERE,name)
        if os.path.exists(p):
            return FileResponse(p, headers={"Cache-Control":"no-store,no-cache"})
    return HTMLResponse("<h2>วางไฟล์ monitor_v3.6.html ไว้ในโฟลเดอร์เดียวกับ server</h2>")

@app.get("/health")
def health():
    age = time.time() - GOLD.get("ts", 0) if GOLD.get("ts") else None
    return {"ok":True,"accounts":len(ACCOUNTS),"history":len(HISTORY),
            "gold":bool(GOLD),"gold_age_sec":round(age,1) if age is not None else None,
            "gold_fail_count":GOLD_STATUS.get("fail_count",0),
            "gold_last_error":GOLD_STATUS.get("last_error",""),
            "news":len(NEWS.get("items",[]))}

# ===== background =====
def loop_gold():
    while True:
        try: poll_gold()
        except Exception as e: print("gold err:",e)
        time.sleep(20)
def loop_news():
    while True:
        try: poll_news()
        except Exception as e: print("news err:",e)
        time.sleep(300)

@app.on_event("startup")
def _startup():
    poll_gold(); poll_news()
    threading.Thread(target=loop_gold, daemon=True).start()
    threading.Thread(target=loop_news, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    print("="*52)
    print("  MT5 Monitor v4.0  —  พอร์ต 8000")
    print(f"  TOKEN : {TOKEN}")
    print(f"  NEWS  : {'เปิด (มี key)' if NEWS_KEY else 'ปิด (ไม่มี key)'}")
    print(f"  requests lib: {'ok' if requests else 'ขาด! ให้รัน install.bat'}")
    print("="*52)
    uvicorn.run(app, host="0.0.0.0", port=8000)
