//+------------------------------------------------------------------+
//| AlexG7Aligned_Smoke.mq5                                          |
//| Smoke-test EA for MT5 Strategy Tester                            |
//| Ports core alexg7aligned path (video2_ghost):                    |
//|   London-NY overlap, close-in D/W AOI, ghost fill at scaled SL   |
//| NOT bit-identical to Python (AOI clustering / multi-pair /       |
//| risk-include-commission sizing differ). Single-chart only.       |
//+------------------------------------------------------------------+
#property copyright "borex smoke"
#property version   "1.00"
#property strict

#include <Trade/Trade.mqh>

input double InpLots          = 0.10;
input double InpMinRR         = 3.0;
input double InpGhostSlMult   = 0.6;
input double InpSlBufferPips  = 16.0;
input double InpAoiPadPips    = 30.0;
input double InpSlTouchPadPips= 30.0;
input double InpClusterPips   = 25.0;
input double InpMinAoiPips    = 8.0;
input double InpMaxAoiPips    = 80.0;
input int    InpMinTouches    = 2;
input int    InpCooldownBars  = 6;
input int    InpGhostExpire   = 72;
input double InpNearSlFrac    = 0.25;
input int    InpMagic         = 77001;
input bool   InpOnlyOverlap   = true;   // 12:00–16:00 UTC

CTrade trade;

struct PipAOI
  {
   double low;
   double high;
   int    kind;      // 1=support, -1=resistance
   int    touches;
   int    last_touch;
   string source_tf;
  };

struct GhostPending
  {
   bool   active;
   int    dir;       // 1 buy, -1 sell
   double planned_entry;
   double stop_loss; // scaled ghost SL (fill price)
   double take_profit;
   double structural_risk; // |planned_entry - structural_sl| before scale
   datetime created_time;
   int    created_bar;
   int    expires_bar;
   bool   saw_near_sl;
  };

GhostPending g_ghost;
int          g_last_signal_bar = -999999;
datetime     g_last_bar_time   = 0;

//+------------------------------------------------------------------+
double PipSize()
  {
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   if(digits == 3 || digits == 5)
      return point * 10.0;
   return point;
  }

//+------------------------------------------------------------------+
bool IsOverlapUTC(datetime t)
  {
   MqlDateTime dt;
   TimeToStruct(t, dt);
   // MT5 tester times are usually broker/server time; for smoke we treat
   // bar time as UTC-like. Adjust InpOnlyOverlap=false if broker offset differs.
   int h = dt.hour;
   return (h >= 12 && h < 16);
  }

//+------------------------------------------------------------------+
void ClearGhost()
  {
   ZeroMemory(g_ghost);
   g_ghost.active = false;
  }

//+------------------------------------------------------------------+
bool PositionOpen()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(!PositionSelectByTicket(PositionGetTicket(i)))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if((int)PositionGetInteger(POSITION_MAGIC) != InpMagic)
         continue;
      return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
bool BuildZonesFromTF(ENUM_TIMEFRAMES tf, const string tag, PipAOI &out[], int &n)
  {
   MqlRates rates[];
   int copied = CopyRates(_Symbol, tf, 0, 400, rates);
   if(copied < 30)
      return false;

   double pip = PipSize();
   double min_w = InpMinAoiPips * pip;
   double max_w = InpMaxAoiPips * pip;
   double cluster = InpClusterPips * pip;

   // Body highs/lows as candidate points
   double pts[];
   int    bars[];
   int    np = 0;
   ArrayResize(pts, copied * 2);
   ArrayResize(bars, copied * 2);
   for(int i = 0; i < copied; i++)
     {
      double bh = MathMax(rates[i].open, rates[i].close);
      double bl = MathMin(rates[i].open, rates[i].close);
      pts[np] = bh; bars[np] = i; np++;
      pts[np] = bl; bars[np] = i; np++;
     }

   // Sort by price (simple insertion — smoke size)
   for(int i = 1; i < np; i++)
     {
      double key = pts[i];
      int bkey = bars[i];
      int j = i - 1;
      while(j >= 0 && pts[j] > key)
        {
         pts[j + 1] = pts[j];
         bars[j + 1] = bars[j];
         j--;
        }
      pts[j + 1] = key;
      bars[j + 1] = bkey;
     }

   bool used[];
   ArrayResize(used, np);
   ArrayInitialize(used, false);

   for(int i = 0; i < np; i++)
     {
      if(used[i])
         continue;
      int j = i;
      while(j + 1 < np && pts[j + 1] - pts[i] <= max_w)
         j++;

      PipAOI best;
      ZeroMemory(best);
      best.touches = 0;

      for(int left = i; left <= j; left++)
        {
         for(int right = left + InpMinTouches - 1; right <= j; right++)
           {
            double lo = pts[left];
            double hi = pts[right];
            double width = hi - lo;
            if(width < min_w || width > max_w)
               continue;

            // unique bars
            int uniq[];
            int nu = 0;
            ArrayResize(uniq, right - left + 1);
            for(int k = left; k <= right; k++)
              {
               bool seen = false;
               for(int u = 0; u < nu; u++)
                  if(uniq[u] == bars[k])
                    { seen = true; break; }
               if(!seen)
                 {
                  uniq[nu++] = bars[k];
                 }
              }
            if(nu < InpMinTouches)
               continue;

            int last_touch = uniq[0];
            for(int u = 1; u < nu; u++)
               if(uniq[u] > last_touch)
                  last_touch = uniq[u];

            double mid = 0.5 * (lo + hi);
            int above = 0, below = 0;
            for(int r = last_touch; r < copied; r++)
              {
               if(rates[r].close > mid)
                  above++;
               else
                  below++;
              }
            int kind = (above < below) ? -1 : 1;

            if(nu > best.touches || (nu == best.touches && last_touch > best.last_touch))
              {
               best.low = lo;
               best.high = hi;
               best.kind = kind;
               best.touches = nu;
               best.last_touch = last_touch;
               best.source_tf = tag;
              }
           }
        }

      if(best.touches >= InpMinTouches)
        {
         bool dup = false;
         for(int z = 0; z < n; z++)
           {
            double mid_z = 0.5 * (out[z].low + out[z].high);
            double mid_b = 0.5 * (best.low + best.high);
            if(MathAbs(mid_z - mid_b) <= cluster && out[z].kind == best.kind)
              { dup = true; break; }
           }
         if(!dup)
           {
            ArrayResize(out, n + 1);
            out[n++] = best;
           }
         for(int k = i; k <= j; k++)
            used[k] = true;
        }
     }
   return n > 0;
  }

//+------------------------------------------------------------------+
bool CollectZones(PipAOI &zones[], int &n)
  {
   n = 0;
   ArrayResize(zones, 0);
   BuildZonesFromTF(PERIOD_D1, "daily", zones, n);
   BuildZonesFromTF(PERIOD_W1, "weekly", zones, n);
   return n > 0;
  }

//+------------------------------------------------------------------+
PipAOI AoiAtClose(const double close_price, const PipAOI &zones[], const int n)
  {
   PipAOI none;
   ZeroMemory(none);
   none.touches = 0;
   double pad = InpAoiPadPips * PipSize();
   for(int i = 0; i < n; i++)
     {
      if(close_price >= zones[i].low - pad && close_price <= zones[i].high + pad)
         return zones[i];
     }
   return none;
  }

//+------------------------------------------------------------------+
double NextOpposingTarget(const double entry, const int dir, const PipAOI &zones[], const int n)
  {
   double best = 0.0;
   double best_dist = 1e100;
   for(int i = 0; i < n; i++)
     {
      double mid = 0.5 * (zones[i].low + zones[i].high);
      if(dir > 0 && zones[i].kind < 0 && mid > entry)
        {
         double d = mid - entry;
         if(d < best_dist)
           { best_dist = d; best = mid; }
        }
      if(dir < 0 && zones[i].kind > 0 && mid < entry)
        {
         double d = entry - mid;
         if(d < best_dist)
           { best_dist = d; best = mid; }
        }
     }
   return best;
  }

//+------------------------------------------------------------------+
bool StopsFromAoiTp(const double entry, const double tp, const int dir,
                    const double structural_sl, const double min_rr,
                    double &sl_out, double &tp_out)
  {
   double reward = MathAbs(tp - entry);
   if(reward <= 0.0)
      return false;
   if(dir > 0)
     {
      double calc_sl = entry - reward / min_rr;
      sl_out = MathMax(calc_sl, structural_sl);
      if(sl_out >= entry)
         return false;
      tp_out = tp;
      return true;
     }
   double calc_sl = entry + reward / min_rr;
   sl_out = MathMin(calc_sl, structural_sl);
   if(sl_out <= entry)
      return false;
   tp_out = tp;
   return true;
  }

//+------------------------------------------------------------------+
void TryQueueGhost(const MqlRates &bar, const int bar_index)
  {
   if(g_ghost.active || PositionOpen())
      return;
   if(bar_index - g_last_signal_bar < InpCooldownBars)
      return;
   if(InpOnlyOverlap && !IsOverlapUTC(bar.time))
      return;

   PipAOI zones[];
   int n = 0;
   if(!CollectZones(zones, n))
      return;

   PipAOI zone = AoiAtClose(bar.close, zones, n);
   if(zone.touches <= 0)
      return;

   int dir = zone.kind; // support→buy, resistance→sell
   double pip = PipSize();
   double structural_sl = (dir > 0)
                          ? zone.low - InpSlBufferPips * pip
                          : zone.high + InpSlBufferPips * pip;

   double entry = bar.close;
   double tp = NextOpposingTarget(entry, dir, zones, n);
   if(tp <= 0.0)
     {
      double risk = MathAbs(entry - structural_sl);
      if(risk <= 0.0)
         return;
      tp = (dir > 0) ? entry + risk * InpMinRR : entry - risk * InpMinRR;
     }

   double sl, tp_final;
   if(!StopsFromAoiTp(entry, tp, dir, structural_sl, InpMinRR, sl, tp_final))
      return;

   // Scale ghost SL toward entry (ghost_sl_mult)
   double dist = MathAbs(entry - sl) * InpGhostSlMult;
   double ghost_sl = (dir > 0) ? entry - dist : entry + dist;

   g_ghost.active = true;
   g_ghost.dir = dir;
   g_ghost.planned_entry = entry;
   g_ghost.stop_loss = ghost_sl;
   g_ghost.take_profit = tp_final;
   g_ghost.structural_risk = MathAbs(entry - sl);
   g_ghost.created_time = bar.time;
   g_ghost.created_bar = bar_index;
   g_ghost.expires_bar = bar_index + InpGhostExpire;
   g_ghost.saw_near_sl = false;
   g_last_signal_bar = bar_index;

   PrintFormat("GHOST queued %s entry=%.5f gsl=%.5f tp=%.5f zone=%s",
               (dir > 0 ? "BUY" : "SELL"), entry, ghost_sl, tp_final, zone.source_tf);
  }

//+------------------------------------------------------------------+
void ResolveGhost(const MqlRates &bar, const int bar_index)
  {
   if(!g_ghost.active)
      return;

   if(bar_index > g_ghost.expires_bar)
     {
      Print("GHOST expired");
      ClearGhost();
      return;
     }

   double pad = InpSlTouchPadPips * PipSize();
   bool tp_hit = (g_ghost.dir > 0)
                 ? (bar.high >= g_ghost.take_profit)
                 : (bar.low <= g_ghost.take_profit);
   if(tp_hit)
     {
      Print("GHOST invalidated (TP first)");
      ClearGhost();
      return;
     }

   bool sl_touch = (g_ghost.dir > 0)
                   ? (bar.low <= g_ghost.stop_loss + pad)
                   : (bar.high >= g_ghost.stop_loss - pad);

   double risk = MathAbs(g_ghost.planned_entry - g_ghost.stop_loss);
   double band = risk * InpNearSlFrac;
   bool in_near = (g_ghost.dir > 0)
                  ? (g_ghost.stop_loss < bar.low && bar.low <= g_ghost.stop_loss + band)
                  : (g_ghost.stop_loss - band <= bar.high && bar.high < g_ghost.stop_loss);
   if(in_near)
      g_ghost.saw_near_sl = true;

   bool left_near = (g_ghost.dir > 0)
                    ? (bar.close > g_ghost.stop_loss + band)
                    : (bar.close < g_ghost.stop_loss - band);
   if(g_ghost.saw_near_sl && left_near && !sl_touch)
     {
      Print("GHOST invalidated (near-miss leave)");
      ClearGhost();
      return;
     }

   if(!sl_touch)
      return;

   // Fill at ghost SL; re-anchor SL/TP distances from fill
   double fill = g_ghost.stop_loss;
   double reward = MathAbs(g_ghost.take_profit - g_ghost.planned_entry);
   double new_sl, new_tp;
   if(g_ghost.dir > 0)
     {
      new_sl = fill - risk;
      new_tp = fill + reward;
     }
   else
     {
      new_sl = fill + risk;
      new_tp = fill - reward;
     }

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(30);
   bool ok = false;
   if(g_ghost.dir > 0)
      ok = trade.Buy(InpLots, _Symbol, 0.0, new_sl, new_tp, "alexg7aligned_smoke");
   else
      ok = trade.Sell(InpLots, _Symbol, 0.0, new_sl, new_tp, "alexg7aligned_smoke");

   PrintFormat("GHOST fill %s fill=%.5f sl=%.5f tp=%.5f ok=%d ret=%d",
               (g_ghost.dir > 0 ? "BUY" : "SELL"), fill, new_sl, new_tp, ok, trade.ResultRetcode());
   ClearGhost();
  }

//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(InpMagic);
   ClearGhost();
   Print("AlexG7Aligned_Smoke ready on ", _Symbol,
         " MinRR=", InpMinRR, " GhostMult=", InpGhostSlMult);
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   // New H1 bar only
   datetime t[];
   if(CopyTime(_Symbol, PERIOD_H1, 0, 1, t) < 1)
      return;
   if(t[0] == g_last_bar_time)
      return;
   g_last_bar_time = t[0];

   // Work on last CLOSED H1 bar (index 1)
   MqlRates bars[];
   if(CopyRates(_Symbol, PERIOD_H1, 1, 1, bars) < 1)
      return;

   int bar_index = Bars(_Symbol, PERIOD_H1) - 2; // approx closed-bar index
   ResolveGhost(bars[0], bar_index);
   if(!g_ghost.active && !PositionOpen())
      TryQueueGhost(bars[0], bar_index);
  }
//+------------------------------------------------------------------+
