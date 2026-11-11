"""Completed partial discovery is a retry, never a negative media proof."""
import math
import re
from reliability import Checkpoint


def source_retry_delay(attempts):
    """The existing source-failure schedule, also used by partial coverage."""
    return min(30*86400,21600*2**min(max(0,attempts-1),7))


class PartialSourceCoverage(Checkpoint):
    def __init__(self,coverage):
        super().__init__('Completed discovery with partial provider coverage','partial_provider_coverage')
        self.coverage=coverage


class SourceCoverage:
    def __init__(self,providers):
        self.providers=tuple(providers)
        if len(self.providers)>8 or any(not re.fullmatch(r'torrentio|mediafusion|jackett:[a-z0-9-]{1,40}',p) for p in self.providers):
            raise ValueError('Invalid coverage providers')
        self.rows={}

    def record(self,provider,status,now,expires,rows=0,reason=None):
        if provider not in self.providers or status not in ('empty','filtered','candidates','unavailable','waiting'):return
        if not all(type(v) in (int,float) and math.isfinite(v) for v in (now,expires)) or expires<=now:return
        row={'provider':provider,'status':status,'observed':now,'expires':expires,'rows':max(0,min(100000,int(rows)))}
        if reason in ('cooldown','network','timeout','invalid_response','busy','capacity','queue_timeout','pacing','recovery') or isinstance(reason,str) and re.fullmatch('http_[1-5][0-9]{2}',reason):row['reason']=reason
        self.rows[provider]=row

    def partial(self,now):
        rows=[self.rows.get(p,{}) for p in self.providers]
        valid=all(r.get('expires',0)>now and r.get('status') in ('empty','filtered','unavailable') for r in rows)
        if not valid or not any(r.get('status') in ('empty','filtered') for r in rows) or not any(r.get('status')=='unavailable' for r in rows):return None
        return {'schema':1,'providers':[dict(r) for r in rows]}
