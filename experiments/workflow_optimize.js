export const meta = {
  name: 'stablecoin-usd1-strategy-optimize',
  description: 'ULTRACODE: iterate USD1 buy-low-sell-high designs, backtest each across an adverse-selection sweep, ADVERSARIALLY VERIFY every claimed win (independent re-implementation to kill backtest-artifact false wins), loop until a verified winner beats 10% hold or we conclude hold-dominance. No fabricated wins.',
  phases: [
    { title: 'Ideate',     detail: 'propose diverse variants aiming to beat 10% hold WITHOUT needing adv<0.4bp' },
    { title: 'Backtest',   detail: 'implement + backtest each variant across adverse-selection sweep' },
    { title: 'Verify',     detail: 'independently re-implement every claimed win; refute backtest artifacts' },
    { title: 'Iterate',    detail: 'refine best variants, re-backtest + re-verify, loop until verified winner or dry' },
    { title: 'Synthesize', detail: 'pick verified winner or declare hold-dominance; produce standalone impl + verdict' },
  ],
}

const WORKDIR = '/workspace/stablecoin_arbi'
const codex = (args && args.codexReview) || '(codex review unavailable)'
const prior = (args && args.context) || ''

const COMMON = `
You are optimizing a stablecoin trading strategy. Work in ${WORKDIR}.
Data: data/USD1USDT_5m.csv + _1h.csv (also USDE/USDTB), ~6.7 months, cols ts,open,high,low,close,volume,turnover.
Engine: backtest/bt_faithful.py — FAITHFUL event-driven backtest of the user's freqtrade strategy, with an adverse-selection (adv bp/side) sweep + UTA-interest accrual. Reuse its load()/patterns. (Codex independently reproduced its USD1 numbers: adv0.5->9.5%, adv1->9.2% — base strategy LOSES to 10% hold.)

LOCKED CONSTRAINTS (do not violate):
- Trade ONLY USD1USDT.
- Holding USD1 long is GOOD (10% APR UTA interest while held). USDT/USDC held = 0% yield.
- MINIMIZE idle USDT time.
- Assume USD1 ALWAYS re-pegs (ignore permanent-depeg tail).
- 0 trading fee. tickSize=1bp price floor.

BENCHMARK: holding USD1 = 10% APR.
WIN CRITERION (strict): TOTAL APR (price+interest) beats 10% at adverse-selection adv >= 0.5 bp/side (NOT just adv=0), with realistic capacity (USD1 ~$2.5M/day; strategy turnover < ~2% of that) and no absurd stuck-capital. Perfect-fill (adv=0) wins do NOT count.

CODEX (GPT) HETEROGENEOUS REVIEW — incorporate valid points, test its ideas:
${codex}

CLAUDE PRIOR ANALYSIS (challenge where warranted):
${prior}
`

const SPEC = { type:'object', additionalProperties:false,
  required:['name','entry_rule','exit_rule','sizing','params','why_beats_hold'],
  properties:{ name:{type:'string'}, entry_rule:{type:'string'}, exit_rule:{type:'string'},
    sizing:{type:'string'}, params:{type:'object'},
    why_beats_hold:{type:'string'} } }

const RESULT = { type:'object', additionalProperties:false,
  required:['variant','apr_adv0','apr_adv05','apr_adv10','beats_hold_at_adv05','capacity_ok','verdict','bt_file','notes'],
  properties:{ variant:{type:'string'}, apr_adv0:{type:'number'}, apr_adv05:{type:'number'},
    apr_adv10:{type:'number'}, beats_hold_at_adv05:{type:'boolean'}, capacity_ok:{type:'boolean'},
    max_stuck_days:{type:'number'}, verdict:{type:'string', enum:['WIN','MARGINAL','LOSE']},
    bt_file:{type:'string'}, notes:{type:'string'} } }

const VERIFY = { type:'object', additionalProperties:false,
  required:['variant','reproduced_apr_adv05','win_survives','refutation'],
  properties:{ variant:{type:'string'}, reproduced_apr_adv05:{type:'number'},
    win_survives:{type:'boolean'},
    refutation:{type:'string', description:'the bug/lookahead/overfit/capacity issue found, or why the win is genuine'} } }

// helper: independent adversarial verification of a claimed win
async function verify(r, tag){
  return agent(`${COMMON}\n\nADVERSARIALLY VERIFY this claimed-winning variant. DO NOT trust its backtest file (${r.bt_file}).\nClaimed result: ${JSON.stringify(r)}\n\nRe-implement the variant's logic FROM SCRATCH in a NEW file backtest/bt_verify_${tag}.py (independent code, your own structure). Run the adv sweep {0,0.5,1.0,1.5}. Then HUNT for reasons the win is fake: lookahead/future-data, fill-rate optimism (orders that would not actually fill), interest double-counting or wrong accrual, capacity violation (turnover >2% of $2.5M/day), or overfitting to this single 6.7-month window (check stability across the first vs second half). Default to win_survives=false if you find ANY material issue or cannot reproduce >10% at adv>=0.5. Return JSON.`,
    {label:`verify:${tag}`.slice(0,48), phase:'Verify', schema:VERIFY})
}

// ---------------- Phase 1: Ideate ----------------
phase('Ideate')
const ANGLES = [
  'BASE-HOLD + THIN SCALP: most capital permanently in USD1 (earning 10%), only a small slice scalps; structurally minimizes idle USDT.',
  'TAKER RE-ENTRY: after a profitable sell, immediately re-buy via marketable order to kill USDT idle time (cost ~1bp, 0 fee).',
  'BIG-DIPS-ONLY: enter only when price >= X bp below rolling mean (sweep X); fewer, cleaner, higher-edge round trips vs adverse selection.',
  'ASYMMETRIC EXIT: passive buy at dips; widen take-profit / scale out on up-moves to capture more than the 1-2bp floor.',
  'IDLE-USDT PARKING (relaxation): park idle USDT in USDe/USDtb (3.5%) between USD1 buys instead of 0% — kills the idle-yield drag that makes trade<hold.',
  'DYNAMIC REFERENCE: faster mean (5m EMA / rolling band) + grid of laddered passive bids to capture more intraday oscillations while staying mostly deployed.',
  'SELL-SIDE LADDER: scale out across multiple take-profit rungs to raise realized capture per cycle without lowering fill probability.',
  'CODEX-PREFERRED: implement the single most promising concrete design from the Codex review; else a novel angle not above.',
]
const specs = (await parallel(ANGLES.map((a,i)=>()=>
  agent(`${COMMON}\n\nDesign ONE concrete, fully-parameterized variant for this angle:\n"${a}"\nExact entry rule, exit rule, sizing, all numeric params, and why it beats 10% hold even at adv>=0.5bp. Implementable. Return JSON.`,
    {label:`ideate:${i}`, phase:'Ideate', schema:SPEC})
))).filter(Boolean)
log(`Ideate: ${specs.length} variants`)

// ---------------- Phase 2+3: Backtest then verify-on-claimed-win (pipelined) ----------------
phase('Backtest')
function backtestSpec(spec, id){
  return agent(`${COMMON}\n\nImplement and BACKTEST this variant on REAL data.\nSPEC: ${JSON.stringify(spec)}\n\n1. Write NEW file backtest/bt_variant_${id}.py (UNIQUE; do NOT edit shared files). Reuse load() from bt_faithful.py. Implement THIS variant exactly. Include UTA interest (10%/yr while holding USD1) and an adverse-selection haircut on EVERY fill (buy at limit*(1+adv/1e4), sell at limit*(1-adv/1e4)).\n2. Run adv in {0,0.5,1.0,1.5} over full ~6.7 months. Print total APR each.\n3. Compare to 10% hold. Check capacity (<2% of $2.5M/day). Note max stuck days.\n4. Return JSON. HONEST: if it loses at adv>=0.5, verdict LOSE. No fabrication.`,
    {label:`bt:${id}`, phase:'Backtest', schema:RESULT})
}
// pipeline: each spec -> backtest -> if claims a win, immediately adversarially verify
let pipe = await pipeline(
  specs.map((s,i)=>({s, id:`r1_${i}`})),
  it => backtestSpec(it.s, it.id).then(r => r ? {...r, _id:it.id} : null),
  r => (r && r.beats_hold_at_adv05 && r.capacity_ok && r.verdict==='WIN')
        ? verify(r, r._id).then(v => ({result:r, verify:v}))
        : {result:r, verify:null}
)
let all = pipe.filter(Boolean)
function verifiedWinners(arr){ return arr.filter(x=>x && x.result && x.verify && x.verify.win_survives) }
log(`Round 1: ${all.length} backtested; verified winners: ${verifiedWinners(all).length}`)

// ---------------- Phase 4: Iterate (loop-until-dry, K<=3) ----------------
phase('Iterate')
let round = 1, dry = 0
while (round < 4 && dry < 2 && verifiedWinners(all).length === 0) {
  round++
  const best = all.map(x=>x.result).filter(Boolean)
    .sort((a,b)=>(b.apr_adv05||-99)-(a.apr_adv05||-99)).slice(0,3)
  if (!best.length) { dry++; continue }
  const prevBest = Math.max(...all.map(x=>x.result&&x.result.apr_adv05||-99))
  const refinedPipe = await pipeline(
    best.map((r,i)=>({r, id:`r${round}_${i}`})),
    it => agent(`${COMMON}\n\nRound ${round} REFINEMENT of the best-so-far variant:\n${JSON.stringify(it.r)}\nImplement a refined version (tune params; fix what dragged it below 10% at adv>=0.5; or graft another variant's winning idea). Write backtest/bt_variant_${it.id}.py, run adv sweep {0,0.5,1,1.5}, report JSON. Honest verdict.`,
      {label:`refine:${it.id}`, phase:'Iterate', schema:RESULT}).then(r=>r?{...r,_id:it.id}:null),
    r => (r && r.beats_hold_at_adv05 && r.capacity_ok && r.verdict==='WIN')
          ? verify(r, r._id).then(v=>({result:r, verify:v}))
          : {result:r, verify:null}
  )
  const refined = refinedPipe.filter(Boolean)
  all.push(...refined)
  const newBest = Math.max(...all.map(x=>x.result&&x.result.apr_adv05||-99))
  if (newBest <= prevBest + 0.05) dry++
  log(`Round ${round}: +${refined.length}; best apr@adv0.5=${newBest.toFixed(2)}%; verified winners=${verifiedWinners(all).length}; dry=${dry}`)
}

// ---------------- Phase 5: Synthesize ----------------
phase('Synthesize')
const ranked = all.map(x=>({...x.result, _verified: !!(x.verify&&x.verify.win_survives), _refutation: x.verify&&x.verify.refutation}))
  .filter(Boolean).sort((a,b)=>(b.apr_adv05||-99)-(a.apr_adv05||-99))
const winners = verifiedWinners(all)
const report = await agent(`${COMMON}\n\nAll variant results (ranked by APR@adv0.5; _verified=passed adversarial re-implementation):\n${JSON.stringify(ranked,null,1)}\n\nVERIFIED WINNERS: ${winners.length}\n\nFINAL honest verdict:\n1. If there is a VERIFIED winner (beats 10% at adv>=0.5, survived independent re-implementation, OK capacity): name it, give its APR curve, and WRITE the final standalone (non-freqtrade) Python implementation to ${WORKDIR}/usd1_strategy_final.py (clear docstring + backtest numbers + the adverse-selection caveat in comments).\n2. If NO verified winner: state plainly that hold-USD1 at 10% dominates and WHY (price edge gated by adverse selection + idle-yield drag; any unverified 'win' was a backtest artifact). Do NOT fabricate.\n3. Give the realistic annualized expectation for the recommended action + the single biggest remaining uncertainty (real adv, measurable only via dryrun.py on live infra).\nConcise markdown.`,
  {label:'synthesize', phase:'Synthesize'})

return { verified_winners: winners.length, ranked, report }
