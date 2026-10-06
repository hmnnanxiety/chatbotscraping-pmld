// Generic public Looker DOM; never reads hidden Angular state/private RPCs.
(root, action = "snapshot") => {
  const text = e => e ? (e.getAttribute("title") || e.textContent || "").trim() : "";
  const componentId = e => [...e.classList].find(x => /^cd-[A-Za-z0-9_-]+$/.test(x));
  if (action === "catalog") {
    const decorative = /(?:text-area|image|rectangle|line-shape|filter-control|date-range-control|group-component)/;
    return [...root.querySelectorAll(".lego-component")].filter(e => {
      const b = e.getBoundingClientRect();
      return b.width && b.height && !decorative.test(e.className);
    }).map(e => ({component_id: componentId(e),
      title: text(e.querySelector(".chart-title")),
      kind: e.classList.contains("simple-table") ? "table" :
        e.querySelector("table thead th, table [role=columnheader]") ? "accessible_table" : "unsupported"}));
  }
  if (action === "scroll" || action === "reset_scroll") {
    const nodes = [...root.querySelectorAll(".tableBody, .tableBody div")];
    const candidates = nodes.filter(e => e.clientHeight > 0 && e.scrollHeight > e.clientHeight + 1 &&
      /auto|scroll|hidden/.test(getComputedStyle(e).overflowY));
    candidates.sort((a,b) => (b.scrollHeight-b.clientHeight) - (a.scrollHeight-a.clientHeight));
    const e = candidates[0];
    if (!e) return false;
    const before = e.scrollTop;
    e.scrollTop = action === "reset_scroll" ? 0 : Math.min(e.scrollHeight, before + Math.max(1,e.clientHeight*0.8));
    return e.scrollTop !== before;
  }
  const label = text(root.querySelector(".pageLabel"));
  const range = label.match(/^(\d+)\s*[-–]\s*(\d+)\s*\/\s*(\d+)$/);
  if (root.classList.contains("simple-table")) {
    const columns = [...root.querySelectorAll(".headerCell .colName")].map(text);
    const segments = [".leftPinnedColsContainer", ".centerColsContainer", ".rightPinnedColsContainer"];
    const indexed = new Map();
    for (let segment=0; segment<segments.length; segment++) {
      for (const row of root.querySelectorAll(segments[segment]+" > .row")) {
        const id = [...row.classList].find(x => /^index-\d+$/.test(x));
        if (!id) continue;
        const index = Number(id.slice(6));
        const blockClass = [...row.classList].find(x => /^block-\d+$/.test(x));
        const block = blockClass ? Number(blockClass.slice(6)) : null;
        const key = `${block}:${index}`;
        if (!indexed.has(key)) indexed.set(key,{index,block,parts:[[],[],[]]});
        indexed.get(key).parts[segment] = [...row.querySelectorAll(":scope > .cell")].map(text);
      }
    }
    // index-* is a recycled DOM slot in live virtualized tables, not a row ID.
    // The unlabelled display ordinal is the authoritative source position.
    return {columns, rows:[...indexed.values()].map(({index,block,parts})=>{
        const cells=parts.flat();
        if(columns[0]==="" && /^\d+\.$/.test(cells[0] || "")) {
          index=Number(cells[0].slice(0,-1))-1;
          block=null;
        }
        return {index,block,cells};
      }).sort((a,b)=>(a.block??0)-(b.block??0)||a.index-b.index)
        .filter(row=>row.cells.length===columns.length),
      range:range ? range.slice(1).map(Number) : null,
      next_disabled: !root.querySelector(".pageForward:not(.disabled):not([aria-disabled=true])"),
      loading:!!root.querySelector("[aria-busy=true],.loading-spinner")};
  }
  const tables = [...root.querySelectorAll("table")].map(table => {
    const columns = [...table.querySelectorAll("thead th, [role=columnheader]")].map(text);
    const rows = [...table.querySelectorAll("tbody tr")].map(r=>[...r.querySelectorAll("td")].map(text));
    return {columns, rows};
  });
  if (!tables.length || tables.some(t=>JSON.stringify(t)!==JSON.stringify(tables[0])))
    return {error:"No single consistent accessible chart table"};
  const table = tables[0];
  return {columns:table.columns,rows:table.rows.map((cells,index)=>({index,cells})),
    range:range ? range.slice(1).map(Number) : [table.rows.length?1:0,table.rows.length,table.rows.length],
    next_disabled:!range || Number(range[2])===Number(range[3]),loading:false};
}
