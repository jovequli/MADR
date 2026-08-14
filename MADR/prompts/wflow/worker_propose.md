Zone "{subbasin_id}" ({station_code}) | Phase: {phase} | Best NSE: {best_nse}
Model: wflow_sbm with integrated kinematic-wave routing. Params are applied to
the model's gridded staticmaps: KsatVer/f/SoilThickness are MULTIPLIED on the
base map (mult); KsatHorFrac/N/N_River and the snow params TT/Cfmax are SET as
per-cell values (absval). Snow: raise TT (>0) to store more precip as snow for a
bigger spring freshet; lower Cfmax to slow/delay the melt into the freshet.

DIAGNOSIS:
{diagnosis_block}

FULL HISTORY (all rounds):
{history_table}
{explore_block}{leader_block}

Bounds: {bounds}
{frozen_note}
Propose parameter changes as JSON.
