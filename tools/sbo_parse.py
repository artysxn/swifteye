"""Parse SBOBET's public euro live page: the odds payload of $P.onUpdate('od', [...]) is a JS array literal; it is
converted to a Python literal (no code is run) and printed per event. usage: sbo_parse.py page.html"""
import re, sys, ast
def payload(t):
    i = t.find("$P.onUpdate('od',")
    if i < 0: return None
    j = i + len("$P.onUpdate('od',"); depth = 0; k = j; q = None
    while k < len(t):
        c = t[k]
        if q:
            if c == '\\': k += 2; continue
            if c == q: q = None
        elif c in "'\"": q = c
        elif c in '[{': depth += 1
        elif c in ']}':
            depth -= 1
            if depth == 0: break
        k += 1
    s = t[j:k + 1]
    s = re.sub(r'(?<=[\[,])\s*(?=[,\]])', 'None', s)          # holes
    s = re.sub(r'\btrue\b', 'True', s); s = re.sub(r'\bfalse\b', 'False', s); s = re.sub(r'\bnull\b', 'None', s)
    return ast.literal_eval(s)
if __name__ == '__main__':
    P = payload(open(sys.argv[1]).read())
    print(type(P), len(P), [type(x).__name__ for x in P][:12])
    for i, x in enumerate(P):
        print(i, repr(x)[:300])
