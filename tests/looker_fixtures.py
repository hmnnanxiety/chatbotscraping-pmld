"""Small synthetic fixtures; counts are NOT production report totals."""
from extractors.looker_studio import report_identity

FIXTURE_ROWS = {"looker-ikm":6, "looker-spbe-2023":7, "looker-spbe-2024":8,
                "looker-spbe-2025":9, "looker-jsp":10}


class FixtureBrowser:
    def __init__(self, source):
        self.source = source
        self.count = FIXTURE_ROWS[source["id"]]
    def __enter__(self):
        return self
    def __exit__(self,*args):
        pass
    def discover(self):
        return [{"component_id":"cd-fixturetable", "page_id":report_identity(self.source["url"])[1],
                 "kind":"table", "title":"Synthetic validation table"}]
    def pages(self,cid):
        assert cid == "cd-fixturetable"
        for start in range(0,self.count,3):
            end = min(start+3,self.count)
            yield {"columns":["ID","Label","Value"], "row_start":start+1,"row_end":end,
                   "total_rows":self.count,
                   "rows":[[str(i),"synthetic "+self.source["id"]+" "+str(i),str(i*10)]
                           for i in range(start,end)]}


def table_html(virtualized=False,buffered=False):
    """Looker-like public DOM with pinned sections, ordinal, pager and virtualization."""
    return """<!doctype html><meta charset=utf-8><style>
    .lego-component {width:900px;height:400px} .tableBody {height:80px;overflow-y:auto}
    .centerColsContainer {position:relative} .row {height:20px;display:flex}
    .cell {width:150px} .headerRow {display:flex} .disabled {pointer-events:none}
    </style><div class="lego-component simple-table cd-fixturetable">
    <div class="headerRow"><div class="headerCell"><div class="colName"></div></div>
    <div class="headerCell"><div class="colName" title="ID">ID</div></div>
    <div class="headerCell"><div class="colName" title="Label">Label</div></div></div>
    <div class="tableBody"><div class="leftPinnedColsContainer"></div><div class="centerColsContainer"></div>
    <div class="rightPinnedColsContainer"></div></div>
    <div class="pageLabel"></div><div class="pageForward" aria-label="Next page">&gt;</div></div>
    <script>
    const virtualized = VIRTUALIZED, buffered=BUFFERED; let page=0;
    const all=Array.from({length:buffered?20:10},(_,i)=>[String(i),"synthetic row "+i]);
    const body=document.querySelector('.tableBody'), center=document.querySelector('.centerColsContainer');
    function draw() {
      const start=virtualized?0:page*3,end=virtualized?all.length:Math.min(start+3,all.length);
      const first=virtualized?Math.max(0,buffered?Math.floor(body.scrollTop/160)*8:Math.floor(body.scrollTop/20)):start;
      const last=virtualized?Math.min(first+(buffered?8:4),end):end;
      center.style.height=virtualized?(all.length*20)+'px':'auto';
      center.innerHTML=all.slice(first,last).map((r,i)=>`<div class="row index-${first+i}"
        style="${virtualized?'position:absolute;top:'+((first+i)*20)+'px':''}">
        <div class="cell">${first+i+1}.</div><div class="cell" title="${r[0]}">${r[0]}</div>
        <div class="cell" title="${r[1]}">${r[1]}</div></div>`).join('');
      document.querySelector('.pageLabel').textContent=`${start+1} - ${end} / ${all.length}`;
      document.querySelector('.pageForward').classList.toggle('disabled',end===all.length);
    }
    document.querySelector('.pageForward').onclick=()=>{page++;body.scrollTop=0;draw()};
    body.onscroll=draw; draw();
    </script>""".replace("VIRTUALIZED",str(virtualized).lower()).replace("BUFFERED",str(buffered).lower())
