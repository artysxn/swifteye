import sys, statistics as st
sys.path.insert(0, '/Users/danielvinichenko/swifteye-src/tools')
import wave_fade as F
G = F.load()
# the usual spread of these games, for comparison
allsp = [g['ask'][i] - g['bid'][i] for g in G for i in range(0, len(g['bid']), 15) if g['bid'][i] > 0]
print('usual spread in play: median %.1fc, middle half %.1f-%.1fc' % (st.median(allsp), sorted(allsp)[len(allsp)//4], sorted(allsp)[3*len(allsp)//4]))
for W, c, T in ((10, 0, 5), (20, 0, 5), (5, 0, 3), (3, 0, 2), (20, 2, 10)):
    R = [r for g in G for r in F.run(g, W, c, T)]
    sp = sorted(r[4] for r in R)
    print('\nwave %dc+, back %dc, target +%dc: %d bets, spread at the bet median %.1fc (middle half %.1f-%.1f)' % (W, c, T, len(R), sp[len(sp)//2], sp[len(sp)//4], sp[3*len(sp)//4]))
    for lo, hi in ((0, 1.5), (1.5, 2.5), (2.5, 4.5), (4.5, 99)):
        X = [r for r in R if lo <= r[4] < hi]
        if len(X) < 15: print('   spread %4.1f-%4.1fc: %4d bets (too few)' % (lo, min(hi, 99), len(X))); continue
        sh = [12 / r[5] for r in X]
        print('   spread %4.1f-%4.1fc: %4d bets | hit %3.0f%% | gross %+5.2fc a share | net %+5.2f $ a bet = %+5.2fc a share | total %+6.0f $' % (lo, hi, len(X), 100.0 * sum(1 for r in X if r[2]) / len(X), st.mean(r[1] for r in X), st.mean(r[0] for r in X), 100 * sum(r[0] for r in X) / sum(sh), sum(r[0] for r in X)))
