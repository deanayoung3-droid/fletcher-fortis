"""Household ('family') vs people the admin manages who are not in the household ('outside').

Existing rows are family. Outside people use the same scan, approval and removal pipeline, but they are
left out of family views, family totals, the family privacy-score average and the family summary email.
"""
FAMILY, OUTSIDE = "family", "outside"
SQL_FAMILY = "COALESCE(circle,'family')!='outside'"

def of(p):
    if not p: return FAMILY
    try: v = p["circle"]
    except (KeyError, IndexError, TypeError): v = None
    return OUTSIDE if v == OUTSIDE else FAMILY

def family(p): return of(p) == FAMILY
def outside(p): return of(p) == OUTSIDE
