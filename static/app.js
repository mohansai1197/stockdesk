"use strict";
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = value => value == null ? "Unknown" : new Intl.NumberFormat("en-IN",{style:"currency",currency:"INR"}).format(value/100);
const day = value => new Date(`${value}T12:00:00`).toLocaleDateString("en-IN",{day:"numeric",month:"short",year:"numeric"});
const stamp = value => new Date(value).toLocaleString("en-IN",{timeZone:"Asia/Kolkata"});
const titles = {
  overview:["Shop overview","Your stock, sales, and the work that needs attention."],
  inventory:["Your inventory","Know what is available, and where to find it."],
  sales:["Sales & returns","Record purchases at the counter and handle returns correctly."],
  purchasing:["Suppliers & purchases","Order stock, then receive it when it reaches your shop."],
  locations:["Stock locations","Move units between your shop, warehouse, and other locations."],
  integrations:["Supplier integrations","Sync invoices, map supplier fields, and review import results."],
  reports:["Sales & gross profit","Understand revenue, refunds, and the cost of what you sold."],
  reorders:["What to order next","Suggestions based on recent sales, lead time, and stock already ordered."],
  activity:["Activity & health","Follow staff actions and investigate rejected requests or imports."],
  admin:["Staff & backups","Control staff access and keep a recoverable copy of your shop."]
};
let user=null, setup=false, products=[], sales=[], suppliers=[], locations=[], summary={};
let page="overview", busy=false, pageOffset=0, filter="", reportDates={start:"",end:""}, renderVersion=0;
let currentPurchases=[], currentProfiles=[], currentBackups=[];
const canManage = () => user && user.role!=="cashier";
const button = (text,action,id="",primary=false) => `<button class="button ${primary?"primary":"secondary"}" data-action="${action}" data-id="${esc(id)}">${text}</button>`;
const field = (label,name,type="text",value="",extra="",wide=false) => `<label class="${wide?"wide":""}">${label}<input name="${name}" type="${type}" value="${esc(value)}" ${extra}></label>`;
const select = (label,name,options,extra="") => `<label>${label}<select name="${name}" ${extra}>${options}</select></label>`;
const locationOptions = value => locations.map(item=>`<option value="${item.id}" ${item.id===Number(value)?"selected":""}>${esc(item.name)}</option>`).join("");
const supplierOptions = (value,optional=false) => `${optional?'<option value="">Not assigned</option>':'<option value="">Choose a supplier</option>'}${suppliers.map(item=>`<option value="${item.id}" ${item.id===Number(value)?"selected":""}>${esc(item.name)}</option>`).join("")}`;
const empty = (title,description) => `<div class="empty"><strong>${title}</strong><p>${description}</p></div>`;
const panel = (title,subtitle,body,action="") => `<section class="panel"><div class="panel-head"><div><h2>${title}</h2><p>${subtitle}</p></div>${action}</div>${body}</section>`;
const table = (headers,rows) => `<div class="table-wrap"><table><thead><tr>${headers.map(text=>`<th>${text}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
const cells = values => `<tr>${values.map(value=>`<td>${value}</td>`).join("")}</tr>`;
const badge = (text,kind="") => `<span class="pill ${kind}">${esc(text)}</span>`;
const metrics = entries => `<section class="metrics" aria-label="Shop totals">${entries.map(([label,value,note],i)=>`<article class="metric ${i===0?"featured":""}"><p class="metric-label">${label}</p><p class="metric-value">${value}</p><div class="metric-note">${note}</div></article>`).join("")}</section>`;
function message(text,error=false){$("#notice").hidden=false;$("#notice").textContent=text;$("#notice").className=error?"error":"";}
function errorMessage(detail){
  if(typeof detail==="string")return detail;
  if(Array.isArray(detail))return detail.map(e=>`${e.loc?.slice(1).join(" · ")||"Input"}: ${e.msg}`).join("\n");
  return `${detail?.message||"Request failed"}${detail?.errors?"\n"+detail.errors.slice(0,15).map(e=>`Row ${e.row}: ${e.error}`).join("\n"):""}`;
}
async function api(path,options={}){
  options.headers={...(options.headers||{})};
  if(options.method && options.method!=="GET" && user)options.headers["X-CSRF-Token"]=user.csrf_token;
  const response=await fetch(path,options);
  const data=await response.json();
  if(!response.ok){if(response.status===401&&!path.startsWith("/api/auth/")){user=null;showAuth(false);}throw new Error(errorMessage(data.detail));}
  return data;
}
const post=(path,body={})=>api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
function showAuth(needsSetup){
  products=[];sales=[];suppliers=[];summary={};$("#content").innerHTML="";$("nav").innerHTML="";$("#notice").hidden=true;
  setup=needsSetup;document.body.classList.add("auth-locked");$("#auth-screen").hidden=false;$("#auth-form").reset();
  $("#auth-title").textContent=setup?"Make this shop yours.":"Welcome back.";
  $("#auth-description").textContent=setup?"Create the first owner account. Your existing products and sales will stay in place.":"Sign in with your staff account.";
  $("#auth-form button").textContent=setup?"Create owner account":"Sign in";
  $("#auth-form [name=password]").autocomplete=setup?"new-password":"current-password";
}
async function loadState(){
  [products,sales,locations,summary]=await Promise.all([api("/api/products?limit=500"),api("/api/sales?limit=50"),api("/api/locations"),api("/api/summary")]);
  suppliers=canManage()?await api("/api/suppliers"):[];
  $("#today").textContent=day(summary.today);reportDates.start||=summary.today.slice(0,8)+"01";reportDates.end||=summary.today;
}
async function enterWorkspace(){
  document.body.classList.remove("auth-locked");$("#auth-screen").hidden=true;
  $("#staff-name").textContent=user.display_name;$("#staff-role").textContent=`${user.role} · Local workspace`;
  const pages=canManage()?Object.keys(titles).filter(key=>key!=="admin"||user.role==="owner"):["overview","inventory","sales"];
  $("nav").innerHTML=pages.map(key=>`<a href="#${key}" data-page="${key}"><span aria-hidden="true">${({overview:"◫",inventory:"▦",sales:"↗",purchasing:"↓",locations:"⇄",integrations:"⇥",reports:"▤",reorders:"◌",activity:"≡",admin:"⚙"})[key]}</span>${({purchasing:"Purchasing",integrations:"Integrations",locations:"Locations",admin:"Staff & backups",reports:"Reports"})[key]||titles[key][0].split(" & ")[0].replace("Shop overview","Overview").replace("Your inventory","Inventory").replace("What to order next","Reorder suggestions")}</a>`).join("");
  await loadState();await render();
}
$("#auth-form").addEventListener("submit",async event=>{
  event.preventDefault();const form=event.target,submit=form.querySelector("button"),error=form.querySelector(".form-error");submit.disabled=true;error.hidden=true;
  try{user=await post(setup?"/api/auth/setup":"/api/auth/login",Object.fromEntries(new FormData(form)));form.reset();await enterWorkspace();}
  catch(exc){error.textContent=exc.message;error.hidden=false;}finally{submit.disabled=false;}
});
$("#logout").addEventListener("click",async()=>{try{await post("/api/auth/logout");user=null;showAuth(false);}catch(exc){message(exc.message,true);}});
function pager(count,limit=20){return `<div class="count-caption pagination"><span>${count===0?"No records":`${pageOffset+1}–${Math.min(pageOffset+limit,count)} of ${count}`}</span><div>${pageOffset?button("← Previous","previous"):""}${pageOffset+limit<count?button("Next →","next"):""}</div></div>`;}
function stockAt(product,location){return product.locations.find(item=>item.id===Number(location))?.quantity||0;}
function salesRows(records){return records.length?table(["SALE / DATE","PRODUCTS","LOCATION","TOTAL",""],records.map(sale=>cells([
  `<strong>#${String(sale.id).padStart(4,"0")}</strong><small class="block muted">${day(sale.sale_date)}</small>`,
  sale.items.map(item=>`${esc(item.name)} × ${item.quantity}${item.returned?` <span class="muted">(${item.returned} returned)</span>`:""}`).join("<br>"),
  esc(sale.location),money(sale.total_paise),canManage()&&sale.items.some(item=>item.returned<item.quantity)?button("Return","return",sale.id):""
]))):empty("No sales yet","Record a sale to reduce stock and start your sales ledger.");}
function categoryBars(data){
  const max=Math.max(1,...data.categories.map(row=>Math.abs(row.revenue_paise)));
  return data.categories.length?`<div class="panel-body">${data.categories.map(row=>`<div class="bar-row"><div class="bar-label"><span>${esc(row.category)}</span><strong>${money(row.revenue_paise)}</strong></div><div class="bar-track" aria-hidden="true"><div class="bar-fill" style="width:${Math.abs(row.revenue_paise)/max*100}%"></div></div><div class="bar-subtitle">${row.units} net units sold</div></div>`).join("")}</div>`:empty("No activity in this date range","Record a sale or choose different report dates.");
}
async function overview(){
  const totals=canManage()?metrics([["Net revenue this month",money(summary.revenue_paise),"Sales minus recorded refunds"],["Gross profit this month",money(summary.gross_profit_paise),summary.uncosted_units?"Some historical purchase costs are missing":"Before operating expenses and taxes"],["Units on hand",summary.units_in_stock,"Across all locations"],["Low-stock products",summary.low_stock_count,"At or below their alert level"]]):metrics([["Products",summary.product_count,"Available catalog"],["Units on hand",summary.units_in_stock,"Across all locations"],["Low-stock products",summary.low_stock_count,"At or below alert level"]]);
  const low=await api("/api/products?low=true&limit=5");
  low.forEach(item=>{if(!products.some(existing=>existing.id===item.id))products.push(item);});
  const lowBody=low.length?`<div class="panel-body">${low.map(item=>`<div class="stock-row"><div><strong>${esc(item.name)}</strong><small>${esc(item.sku)}</small></div><div class="stock-count"><strong>${item.stock}</strong><small>units remaining</small></div>${canManage()?button("Restock","restock",item.id):""}</div>`).join("")}</div>`:empty("Your shelves look good","Products running low will appear here.");
  return `${!summary.product_count?`<section class="welcome"><div><h2>Start with your first product.</h2><p>Add a product or import a supplier file. Then receive stock and record a sale.</p></div>${canManage()?button("Import products","import","",true):""}</section>`:""}${totals}<div class="grid-two">${canManage()?panel("What’s selling","Net revenue by category this month",categoryBars(summary)):""}${panel("Stock to watch","Across all stock locations",lowBody)}</div>${panel("Latest sales","Your most recently recorded purchases",salesRows(sales.slice(0,5)),`<a class="text-button" href="#sales">All sales ↗</a>`)}`;
}
async function inventory(){
  const response=await fetch(`/api/products?limit=20&offset=${pageOffset}&search=${encodeURIComponent(filter)}`);
  if(!response.ok)throw new Error("Could not load inventory. Sign in again if your session expired.");
  const records=await response.json(),count=Number(response.headers.get("X-Total-Count"));
  records.forEach(item=>{const index=products.findIndex(existing=>existing.id===item.id);if(index<0)products.push(item);else products[index]=item;});
  return panel("Product catalog","Stock is tracked separately at each location.",`<form id="search-form" class="toolbar"><input aria-label="Search product or SKU" name="search" type="search" value="${esc(filter)}" placeholder="Search product or SKU…"><button class="button secondary">Search</button></form>${records.length?table(["PRODUCT","PRICE / COST","STOCK BY LOCATION","STATUS","ACTIONS"],records.map(item=>cells([
    `<strong>${esc(item.name)}</strong><small class="block muted">${esc(item.sku)} · ${esc(item.category)}</small>`,
    `${money(item.price_paise)}${canManage()?`<small class="block muted">Cost: ${money(item.cost_paise)}</small>`:""}`,
    item.locations.map(location=>`${esc(location.name)}: <strong>${location.quantity}</strong>`).join("<br>"),
    badge(item.stock===0?"Out of stock":item.stock<=item.low_stock_at?"Low stock":"In stock",item.stock<=item.low_stock_at?"low":""),
    `<div class="row-actions">${canManage()?`${button("Edit","edit",item.id)}${button("Restock","restock",item.id)}`:""}${button("History","history",item.id)}</div>`
  ]))):empty("No matching products","Add a product or search for another SKU.")}${pager(count)}`,canManage()?button("Import file","import"):"");
}
async function salesPage(){
  const response=await fetch(`/api/sales?limit=20&offset=${pageOffset}`);if(!response.ok)throw new Error("Could not load sales");
  sales=await response.json();const count=Number(response.headers.get("X-Total-Count"));
  const returns=canManage()?await api("/api/returns"):[];
  return panel("Sales ledger","Recorded totals use the price saved at the time of sale.",salesRows(sales)+pager(count))+(canManage()?panel("Returns & refund records","These are bookkeeping records; the app does not transfer money.",returns.length?table(["RETURN / SALE","DATE","REASON","REFUND"],returns.map(item=>cells([`#${item.id} / Sale #${item.sale_id}`,day(item.return_date),esc(item.reason),money(item.total_paise)]))):empty("No returns recorded","Use Return beside a sale to record one.")):"");
}
async function purchasing(){
  currentPurchases=await api("/api/purchases");
  return panel("Purchase orders","Ordering does not change stock. Receiving the full order does.",currentPurchases.length?table(["ORDER","SUPPLIER / LOCATION","PRODUCTS","TOTAL COST","STATUS"],currentPurchases.map(order=>cells([
    `#${order.id}`,`${esc(order.supplier)}<small class="block muted">${esc(order.location)}</small>`,order.items.map(item=>`${esc(item.name)} × ${item.quantity}`).join("<br>"),money(order.total_paise),order.status==="ordered"?button("Receive order","receive",order.id):badge("Received")
  ]))):empty("No purchase orders","Add a supplier and order stock for your products."),button("New purchase","purchase","",true))+panel("Your suppliers","Lead time is used in reorder suggestions.",suppliers.length?table(["SUPPLIER","EMAIL","LEAD TIME"],suppliers.map(item=>cells([esc(item.name),esc(item.email)||"—",`${item.lead_days} days`]))):empty("No suppliers yet","Add the people or companies that supply your shop."),button("Add supplier","supplier"));
}
async function locationsPage(){
  return panel("Locations","New products and old stock start in Main shop unless you choose another location.",table(["LOCATION",""],locations.map(item=>cells([esc(item.name),`<span class="muted">Location #${item.id}</span>`]))),button("Add location","location"))+panel("Move stock safely","Transfers decrease one location and increase the other in the same transaction.",`<div class="panel-body"><p>Total shop stock stays unchanged. Each side of the transfer appears in stock history.</p>${button("Transfer stock","transfer","",true)}</div>`);
}
async function integrations(){
  const imports=await api("/api/imports");currentProfiles=await api("/api/mapping-profiles");
  return (await accountingPanel())+`<section class="welcome"><div><h2>Different headings. One clean catalog.</h2><p>Import CSV, JSON, or XML. Preview supplier records, map their fields, and validate the complete file before saving.</p></div><div class="actions">${button("Import file","import","",true)}${button("Connect demo supplier","supplier-api")}</div></section>`+panel("Import history","Rejected imports keep their errors and mapping; no products are partially saved.",imports.length?table(["BATCH / SOURCE","RESULT","ROWS","DETAILS"],imports.map(item=>cells([
    `#${item.id}<small class="block muted">${esc(item.source)}</small>`,badge(item.status,item.status==="rejected"?"out":""),item.imported,
    `<details><summary>${item.errors.length?`${item.errors.length} errors`:"Field mapping"}</summary><div class="details-body">${item.errors.map(error=>`<p>Row ${error.row}: ${esc(error.error)}</p>`).join("")}<pre>${esc(JSON.stringify(item.mapping,null,2))}</pre></div></details>`
  ]))):empty("No imports yet","Try the supplied sample or the fictional supplier API."))+panel("Saved mapping profiles","Saving the same profile name creates a new version.",currentProfiles.length?table(["PROFILE","VERSION",""],currentProfiles.map(item=>cells([esc(item.name),item.version,`<details><summary>View mapping</summary><pre>${esc(JSON.stringify(item.mapping,null,2))}</pre></details>`]))):empty("No saved profiles","Preview a file and save its field mapping for future uploads."));
}
async function reports(){
  const data=await api(`/api/reports?${new URLSearchParams(reportDates)}`);
  return `<form id="report-form" class="report-controls">${field("From","start","date",reportDates.start,"required")}${field("To","end","date",reportDates.end,"required")}<button class="button primary">Update report</button><a class="button secondary" href="/api/reports/export?${new URLSearchParams(reportDates)}">Download CSV</a></form>`+
    metrics([["Gross sales",money(data.gross_revenue_paise),`${data.sales_count} purchases recorded`],["Recorded refunds",money(data.refund_paise),"By the date of the return"],["Net revenue",money(data.revenue_paise),"Gross sales minus recorded refunds"],["Gross profit",money(data.gross_profit_paise),data.uncosted_units?"Historical purchase costs are missing":"Net revenue minus cost of goods sold"]])+
    `<p class="helper">Gross profit excludes rent, wages, payment fees, and taxes. Older sales without costs remain unknown. Returned sellable units reverse their original cost; damaged returns do not.</p>`+
    `<div class="grid-two">${panel("Net revenue by category",`${day(data.start)} – ${day(data.end)}`,categoryBars(data))}${panel("Daily net revenue","Refunds are included on their return date.",data.daily.length?table(["DATE","NET REVENUE"],data.daily.map(item=>cells([day(item.sale_date),money(item.revenue_paise)]))):empty("No activity","Choose a different date range."))}</div>`+
    panel("Top-selling products","Gross units sold in the selected range, before returns.",data.top_products.length?table(["PRODUCT","UNITS SOLD","GROSS REVENUE"],data.top_products.map(item=>cells([esc(item.name),item.units,money(item.revenue_paise)]))):empty("No sales","Record a sale to populate this report."));
}
async function reorders(){
  const records=await api("/api/reorders");
  return panel("Reorder suggestions","Target: 30-day daily sales × (supplier lead days + 7 buffer days), with at least the alert threshold + 1 unit.",records.length?table(["PRODUCT / SUPPLIER","AVAILABLE / ORDERED","TARGET","SUGGESTED",""],records.map(item=>cells([
    `${esc(item.name)}<small class="block muted">${esc(item.supplier)||"No supplier assigned"} · ${item.lead_days}-day lead time</small>`,`${item.stock} / ${item.on_order}`,item.target_stock,item.suggested_quantity,button("Create purchase","reorder",item.id)
  ]))):empty("No reorder suggestions","Current stock and pending purchases cover the calculated targets."))+`<p class="helper">Suggestions do not place orders automatically. Review demand and supplier availability before purchasing.</p>`;
}
async function activity(){
  const [events,health,ops]=await Promise.all([api(`/api/activity?offset=${pageOffset}&limit=20`),api("/api/health"),api("/api/operations")]);
  return metrics([["Requests since restart",ops.requests,`${Math.floor(ops.uptime_seconds/60)} minutes uptime`],["Rejected requests",ops.rejected_requests,"Includes validation and permission failures"],["Server errors",ops.server_errors,"Since this server started"],["Data consistency issues",ops.stock_mismatches.length+ops.sale_total_mismatches,"Stock totals and saved sale arithmetic"]])+`<div class="health-banner"><span class="status-dot"></span>Database connected · App ${esc(health.version)} · Failed requests and rejected imports appear below.</div>`+panel("Activity log","Password hashes, session cookies, and submitted passwords are never included here.",events.length?table(["TIME (IST)","STAFF","ACTION / RECORD","DETAILS"],events.map(item=>cells([stamp(item.created_at),esc(item.actor),`${esc(item.action)}<small class="block muted">${esc(item.entity)}</small>`,`<pre>${esc(JSON.stringify(item.details,null,2))}</pre>`]))):empty("No activity","Actions will appear as staff use the app."))+`<div class="pagination">${pageOffset?button("← Previous","previous"):""}${events.length===20?button("Next →","next"):""}</div>`;
}
async function admin(){
  const [staffList,backups]=await Promise.all([api("/api/staff"),api("/api/backups")]);currentBackups=backups;
  return panel("Staff accounts","Owners manage staff and backups. Managers handle shop operations. Cashiers view stock and record sales.",table(["STAFF","ROLE","STATUS",""],staffList.map(item=>cells([
    `${esc(item.display_name)}<small class="block muted">${esc(item.username)}</small>`,esc(item.role),badge(item.active?"Active":"Inactive",item.active?"":"out"),item.active&&item.id!==user.id?button("Deactivate","deactivate",item.id):""
  ]))),button("Add staff","staff"))+panel("Database backups","Create a copy before major changes. Restore replaces current records and signs out every staff member.",backups.length?table(["CREATED (IST)","SIZE","BACKUP","ACTIONS"],backups.map(item=>cells([
    stamp(item.created_at*1000),`${Math.ceil(item.bytes/1024)} KiB`,`<span class="backup-name">${esc(item.name)}</span>`, `<div class="row-actions"><a class="button secondary" href="/api/backups/${encodeURIComponent(item.name)}">Download</a>${[2,3].includes(item.schema_version)?button("Restore","restore",item.name):badge("Older archive")}</div>`
  ]))):empty("No backups yet","Create your first recoverable copy."),button("Create backup","backup","",true));
}
async function render(){
  if(!user)return;const version=++renderVersion;
  const hash=location.hash.slice(1);page=Object.hasOwn(titles,hash)?hash:"overview";
  if((!canManage()&&!["overview","inventory","sales"].includes(page))||(page==="admin"&&user.role!=="owner"))page="overview";
  $("#page-title").textContent=titles[page][0];$("#page-description").textContent=titles[page][1];
  $$("nav a").forEach(link=>{if(link.dataset.page===page)link.setAttribute("aria-current","page");else link.removeAttribute("aria-current");});
  $("#header-actions").innerHTML=`${canManage()?button("+ Add product","product"):""}${button("↗ Record sale","sale","",true)}`;
  $("#content").innerHTML='<p class="loading">Loading workspace…</p>';
  try{
    const html=await ({overview,inventory,sales:salesPage,purchasing,locations:locationsPage,integrations,reports,reorders,activity,admin})[page]();
    if(version!==renderVersion||!user)return;$("#content").innerHTML=html;
    $("#search-form")?.addEventListener("submit",event=>{event.preventDefault();filter=new FormData(event.target).get("search");pageOffset=0;render();});
    $("#report-form")?.addEventListener("submit",event=>{event.preventDefault();reportDates=Object.fromEntries(new FormData(event.target));render();});
  }catch(exc){if(version===renderVersion){$("#content").innerHTML=empty("Could not load this page",esc(exc.message));message(exc.message,true);}}
}
function openModal(title,html,action,description="",saveLabel="Save"){
  const dialog=$("#work-dialog");
  dialog.innerHTML=`<form id="work-form"><div class="dialog-head"><div><p class="eyebrow">SHOP OPERATIONS</p><h2 id="dialog-title">${title}</h2></div><button class="close" type="button" data-close aria-label="Close dialog">×</button></div>${description?`<p class="helper">${description}</p>`:""}<fieldset>${html}</fieldset><p class="form-error" role="alert" hidden></p><div class="dialog-actions"><button type="button" class="button secondary" data-close>Cancel</button><button class="button primary" type="submit">${saveLabel}</button></div></form>`;
  $("#work-form").onsubmit=async event=>{
    event.preventDefault();if(busy)return;busy=true;const form=event.target,fd=new FormData(form),error=form.querySelector(".form-error"),fieldset=form.querySelector("fieldset"),submit=form.querySelector('[type="submit"]');error.hidden=true;
    try{
      // Read line items before disabling the form; disabled controls are excluded from FormData.
      const task=action(fd,form);fieldset.disabled=true;submit.disabled=true;
      const result=await task;dialog.close();
      if(result?.signout){user=null;showAuth(false);return;}
      await loadState();await render();message(typeof result==="string"?result:result?.message||"Saved successfully.",!!result?.error);
    }catch(exc){error.textContent=exc.message;error.hidden=false;}finally{busy=false;fieldset.disabled=false;submit.disabled=false;}
  };
  dialog.showModal();return dialog;
}
const numeric=(fd,name)=>Number(fd.get(name));
function productForm(item=null){
  const edit=!!item;
  openModal(edit?`Edit ${esc(item.name)}`:"Add a product",`<div class="form-grid">${edit?"":`${field("Product name","name","text","","required maxlength=100",true)}${field("SKU","sku","text","","required maxlength=32 pattern='[A-Za-z0-9][A-Za-z0-9_-]{0,31}'")}${field("Category","category","text","","required maxlength=50")}`}${field("Selling price (₹)","price","number",edit?(item.price_paise/100).toFixed(2):"","required min=0.01 max=1000000 step=0.01")}${field("Purchase cost per unit (₹)","cost","number",item?.cost_paise!=null?(item.cost_paise/100).toFixed(2):"","min=0.01 max=1000000 step=0.01")}${edit?"":`${field("Opening stock","stock","number",0,"required min=0 max=1000000 step=1")}${select("Opening-stock location","location_id",locationOptions(1))}`}${field("Low-stock alert at","low_stock_at","number",item?.low_stock_at??5,"required min=0 max=1000000 step=1")}${select("Preferred supplier","supplier_id",supplierOptions(item?.supplier_id,true))}</div><p class="helper">Leave purchase cost blank if unknown. Changing it updates current stock valuation; it never rewrites historical sale costs.</p>`,async fd=>{
    const body={price:fd.get("price"),low_stock_at:numeric(fd,"low_stock_at")};if(fd.get("cost"))body.cost=fd.get("cost");if(fd.get("supplier_id"))body.supplier_id=numeric(fd,"supplier_id");
    if(!edit)Object.assign(body,{name:fd.get("name"),sku:fd.get("sku"),category:fd.get("category"),stock:numeric(fd,"stock"),location_id:numeric(fd,"location_id")});
    await post(edit?`/api/products/${item.id}/update`:"/api/products",body);return edit?"Product updated. Previous values are recorded in Activity.":"Product added.";
  });
}
function productOptions(selected){return '<option value="">Choose a product</option>'+products.map(item=>`<option value="${item.id}" ${Number(selected)===item.id?"selected":""}>${esc(item.name)} · ${esc(item.sku)}</option>`).join("");}
function addOrderLine(kind,prefill={}){
  if($$(".order-line").length>=20)return;
  const line=document.createElement("div");line.className="order-line";
  line.innerHTML=`<div class="product-lookup">${field("Find product","lookup","search","","placeholder='Search by name or SKU'")}${select("Product","product_id",productOptions(prefill.id),"required")}</div>${field("Quantity","quantity","number",prefill.quantity||1,"required min=1 max=1000000 step=1")}${kind==="purchase"?field("Unit cost (₹)","cost","number",prefill.cost_paise?(prefill.cost_paise/100).toFixed(2):"","required min=0.01 max=1000000 step=0.01"):""}<button type="button" class="close remove-line" aria-label="Remove product">×</button>`;
  $("#order-lines").append(line);
  let timer;
  line.querySelector('[name="lookup"]').addEventListener("input",event=>{
    clearTimeout(timer);const input=event.target.value;
    timer=setTimeout(async()=>{try{const found=await api(`/api/products?search=${encodeURIComponent(input)}&limit=50`);found.forEach(item=>{if(!products.some(existing=>existing.id===item.id))products.push(item);});line.querySelector("select").innerHTML='<option value="">Choose a product</option>'+found.map(item=>`<option value="${item.id}">${esc(item.name)} · ${esc(item.sku)}</option>`).join("");updateLineTotal(kind);}catch(exc){message(exc.message,true);}},250);
  });
  line.querySelector("select").addEventListener("change",()=>{if(kind==="purchase"){const item=products.find(item=>item.id===Number(line.querySelector("select").value));line.querySelector('[name="cost"]').value=item?.cost_paise!=null?(item.cost_paise/100).toFixed(2):"";}updateLineTotal(kind);});
  line.addEventListener("input",()=>updateLineTotal(kind));updateLineTotal(kind);
}
function updateLineTotal(kind){
  let total=0;$$(".order-line").forEach(line=>{const product=products.find(item=>item.id===Number(line.querySelector("select").value)),qty=Number(line.querySelector('[name="quantity"]').value);total+=(kind==="purchase"?Math.round(Number(line.querySelector('[name="cost"]').value)*100):product?.price_paise||0)*qty;});
  if($("#order-total"))$("#order-total").textContent=money(total);
}
function orderModal(kind,prefill={}){
  if(!products.length){message("Add a product before recording a sale or purchase.",true);return;}
  if(kind==="purchase"&&!suppliers.length){message("Add a supplier first.",true);return;}
  const purchase=kind==="purchase";
  openModal(purchase?"Create purchase order":"Record a sale",`<div class="form-grid">${purchase?select("Supplier","supplier_id",supplierOptions(prefill.supplier_id),"required"):field("Sale date","sale_date","date",summary.today,`required max=${summary.today}`)}${select(purchase?"Receive into":"Sell from","location_id",locationOptions(1))}</div><div id="order-lines"></div><button type="button" class="text-button" id="add-order-line">+ Add another product</button><div class="sale-total"><span>${purchase?"Purchase cost":"Sale total"}</span><strong id="order-total"></strong></div>`,async(fd,form)=>{
    const items=[...form.querySelectorAll(".order-line")].map(line=>{const item={product_id:Number(line.querySelector("select").value),quantity:Number(line.querySelector('[name="quantity"]').value)};if(purchase)item.cost=line.querySelector('[name="cost"]').value;return item;});
    const body={items,location_id:numeric(fd,"location_id")};if(purchase)body.supplier_id=numeric(fd,"supplier_id");else body.sale_date=fd.get("sale_date");
    const result=await post(purchase?"/api/purchases":"/api/sales",body);return purchase?`Purchase #${result.id} ordered. Stock changes when you receive it.`:`Sale #${result.id} saved for ${money(result.total_paise)}. Stock updated.`;
  },purchase?"Receive the entire order when it arrives. Partial receipts are outside this version.":"The server checks stock at the selected location and uses saved selling prices.");
  addOrderLine(kind,prefill);$("#add-order-line").onclick=()=>addOrderLine(kind);
}
async function importModal(){
  currentProfiles=await api("/api/mapping-profiles");
  const standard={sku:"sku",name:"name",category:"category",price:"price",cost:"cost",stock:"stock",low_stock_at:"low_stock_at"};
  const alternate={sku:"Item Code",name:"Product Name",category:"Department",price:"Sell Price",cost:"Buy Price",stock:"Available",low_stock_at:"Reorder At"};
  let mapping=standard;
  openModal("Import supplier products",`<div class="form-grid">${field("Product file","file","file","","required accept='.csv,.json,.xml'",true)}${select("File format","kind",'<option value="csv">CSV</option><option value="json">JSON</option><option value="xml">XML</option>')}${select("Supplier","supplier_id",supplierOptions(null,true))}${select("Stock location","location_id",locationOptions(1))}${select("Starting mapping","profile",`<option value="standard">Standard field names</option><option value="supplier">Supplier column names</option>${currentProfiles.map(item=>`<option value="${item.id}">${esc(item.name)} v${item.version}</option>`).join("")}`)}</div><div class="actions import-actions"><button type="button" class="button secondary" id="preview-file">Preview & map fields</button><a class="text-button" href="/api/sample.csv" download>Download sample CSV</a></div><div id="import-preview"></div><div class="form-grid">${field("Save mapping as (optional)","profile_name","text","","maxlength=80",true)}</div><button class="text-button" type="button" id="save-mapping">Save mapping profile</button><p class="helper">Up to 500 products / 256 KiB. Existing SKUs are rejected. No rows are saved if any row fails.</p>`,async(fd,form)=>{
    if(!form.querySelector(".mapping-select"))throw new Error("Preview the file and check its field mapping first.");
    const selected=Object.fromEntries([...form.querySelectorAll(".mapping-select")].map(input=>[input.dataset.field,input.value]));
    fd.set("mapping",JSON.stringify(selected));fd.delete("profile");fd.delete("profile_name");if(!fd.get("supplier_id"))fd.delete("supplier_id");
    const result=await api("/api/products/import",{method:"POST",body:fd});return `${result.imported} products imported. Batch #${result.batch_id} saved.`;
  },"Map the supplier's headings to StockDesk fields. The preview does not change your catalog.","Validate & import");
  $("#work-form [name=profile]").onchange=event=>{mapping=event.target.value==="standard"?standard:event.target.value==="supplier"?alternate:currentProfiles.find(item=>item.id===Number(event.target.value)).mapping;$("#import-preview").innerHTML="";};
  $("#work-form [name=file]").onchange=()=>{$("#import-preview").innerHTML="";};
  $("#work-form [name=kind]").onchange=()=>{$("#import-preview").innerHTML="";};
  $("#preview-file").onclick=async()=>{
    const form=$("#work-form"),error=form.querySelector(".form-error");error.hidden=true;
    try{const fd=new FormData(form);if(!fd.get("file").size)throw new Error("Choose a file first.");const result=await api("/api/imports/preview",{method:"POST",body:fd});
      $("#import-preview").innerHTML=`<p class="helper">${result.total} records · First ${result.rows.length} shown</p>${table(result.headers.map(esc),result.rows.map(row=>cells(result.headers.map(key=>esc(row[key])))))}<h3 class="mapping-heading">Match supplier fields</h3><div class="form-grid">${result.fields.map(key=>`<label>${esc(key)}<select class="mapping-select" data-field="${key}" ${["cost","low_stock_at"].includes(key)?"":"required"}><option value="">${key==="cost"?"Unknown cost":key==="low_stock_at"?"Default: 5":"Choose source column"}</option>${result.headers.map(header=>`<option value="${esc(header)}" ${mapping[key]===header?"selected":""}>${esc(header)}</option>`).join("")}</select></label>`).join("")}</div>`;
    }catch(exc){error.textContent=exc.message;error.hidden=false;}
  };
  $("#save-mapping").onclick=async()=>{try{const form=$("#work-form"),fd=new FormData(form),inputs=[...form.querySelectorAll(".mapping-select")];if(!inputs.length||!fd.get("profile_name").trim())throw new Error("Preview a file and enter a profile name first.");const result=await post("/api/mapping-profiles",{name:fd.get("profile_name"),supplier_id:fd.get("supplier_id")?numeric(fd,"supplier_id"):null,mapping:Object.fromEntries(inputs.map(input=>[input.dataset.field,input.value]))});$("#save-mapping").textContent=`Mapping saved · Version ${result.version}`;}catch(exc){const error=$("#work-form .form-error");error.textContent=exc.message;error.hidden=false;}};
}
async function action(name,id){
  if(name.startsWith("invoice-"))return accountingAction(name,id);
  const product=products.find(item=>item.id===Number(id));
  if(name==="product")return productForm();
  if(name==="edit")return productForm(product);
  if(name==="sale")return orderModal("sale");
  if(name==="purchase")return orderModal("purchase");
  if(name==="reorder"){const row=(await api("/api/reorders")).find(item=>item.id===Number(id));return orderModal("purchase",{...row,quantity:row.suggested_quantity});}
  if(name==="import")return importModal();
  if(name==="restock")return openModal(`Restock ${esc(product.name)}`,`<div class="form-grid">${select("Location","location_id",locationOptions(1))}${field("Units to add","quantity","number",1,"required min=1 max=1000000 step=1")}${field("Unit cost (₹, optional)","cost","number","","min=0.01 max=1000000 step=0.01",true)}</div>`,async fd=>{await post(`/api/products/${id}/restock`,{quantity:numeric(fd,"quantity"),location_id:numeric(fd,"location_id"),cost:fd.get("cost")||null});return "Stock added. Use purchase orders when you want supplier and order records.";});
  if(name==="history"){
    const rows=await api(`/api/products/${id}/movements`);
    openModal(`Stock history`,rows.length?table(["TIME (IST)","LOCATION / ACTION","CHANGE","STAFF"],rows.map(item=>cells([stamp(item.created_at),`${esc(item.location)}<small class="block muted">${esc(item.reason)}</small>`,`${item.change>0?"+":""}${item.change}`,esc(item.actor)]))):empty("No stock changes yet","Stock activity will appear here."),async()=>"", "Latest 100 stock changes.","Done");return;
  }
  if(name==="supplier")return openModal("Add a supplier",`<div class="form-grid">${field("Supplier name","name","text","","required maxlength=100",true)}${field("Email (optional)","email","email","","maxlength=120")}${field("Delivery lead time (days)","lead_days","number",7,"required min=1 max=90 step=1")}</div>`,async fd=>{await post("/api/suppliers",{name:fd.get("name"),email:fd.get("email"),lead_days:numeric(fd,"lead_days")});return "Supplier added.";});
  if(name==="location")return openModal("Add a stock location",field("Location name","name","text","","required maxlength=60"),async fd=>{await post("/api/locations",{name:fd.get("name")});return "Location added.";});
  if(name==="transfer"){
    // Refresh stock before displaying reference counts for the selected product.
    products=await api("/api/products?limit=500");
    const dialog=openModal("Transfer stock",`<div class="form-grid">${select("Product","product_id",productOptions(),"required")}${field("Quantity","quantity","number",1,"required min=1 max=1000000 step=1")}<div>${select("From location","from_location",locationOptions(1),'aria-describedby="from-stock-reference"')}<p id="from-stock-reference" class="helper stock-reference" role="status" aria-live="polite" aria-atomic="true"></p></div><div>${select("To location","to_location",locationOptions(2),'aria-describedby="to-stock-reference"')}<p id="to-stock-reference" class="helper stock-reference" role="status" aria-live="polite" aria-atomic="true"></p></div></div>`,async fd=>{
      await post("/api/transfers",Object.fromEntries([...fd].map(([key,value])=>[key,Number(value)])));
      return "Stock transferred. Total inventory is unchanged.";
    });
    const form=dialog.querySelector("form");
    function updateStockReferences(){
      const selected=products.find(item=>item.id===Number(form.elements.product_id.value));
      for(const [fieldName,referenceId] of [["from_location","from-stock-reference"],["to_location","to-stock-reference"]]){
        const reference=dialog.querySelector(`#${referenceId}`);
        if(!selected){reference.textContent="Select a product to see current stock.";continue;}
        const quantity=stockAt(selected,form.elements[fieldName].value);
        reference.textContent=`Current stock: ${new Intl.NumberFormat("en-IN").format(quantity)} ${quantity===1?"unit":"units"}.`;
      }
    }
    for(const fieldName of ["product_id","from_location","to_location"]){
      form.elements[fieldName].addEventListener("change",updateStockReferences);
    }
    updateStockReferences();
    return;
  }
  if(name==="receive"){
    const order=currentPurchases.find(item=>item.id===Number(id));
    return openModal(`Receive purchase #${id}`,`<p>${esc(order.supplier)} → ${esc(order.location)}</p><ul>${order.items.map(item=>`<li>${esc(item.name)} × ${item.quantity}</li>`).join("")}</ul><label class="checkbox-label"><input type="checkbox" required>I received the full order.</label>`,async()=>{await post(`/api/purchases/${id}/receive`);return "Order received. Stock and average purchase costs updated.";},"An order can only be received once.","Receive stock");
  }
  if(name==="return"){
    const sale=sales.find(item=>item.id===Number(id)),available=sale.items.filter(item=>item.returned<item.quantity);
    return openModal(`Return from sale #${id}`,available.map(item=>`<div class="return-line" data-item="${item.id}"><strong>${esc(item.name)}</strong><small class="block muted">${item.quantity-item.returned} remaining · ${money(item.unit_price_paise)} each</small>${field("Quantity to return","return_qty","number",0,`min=0 max=${item.quantity-item.returned} step=1 required`)}<label class="checkbox-label"><input name="restock" type="checkbox" checked>Sellable: put back in stock</label></div>`).join("")+field("Reason","reason","text","","required minlength=3 maxlength=200"),async(fd,form)=>{
      const items=[...form.querySelectorAll(".return-line")].map(line=>({sale_item_id:Number(line.dataset.item),quantity:Number(line.querySelector('[name=return_qty]').value),restock:line.querySelector('[name=restock]').checked})).filter(item=>item.quantity>0);
      if(!items.length)throw new Error("Enter a positive quantity for at least one product.");const result=await post(`/api/sales/${id}/returns`,{items,reason:fd.get("reason")});return `Return recorded: ${money(result.refund_paise)} refund due. No payment was transferred.`;
    },"Returns cannot exceed the units sold. Uncheck Sellable for damaged goods.","Record return");
  }
  if(name==="supplier-api"){
    if(!suppliers.length){message("Add a supplier in Purchasing first.",true);return;}
    return openModal("Connect demo supplier",`<div class="form-grid">${select("Associate with supplier","supplier_id",supplierOptions(),"required")}${select("Stock location","location_id",locationOptions(1))}</div><p class="helper">Fictional supplier at 127.0.0.1:8701. Its JSON uses Item Code, Product Name, and other alternate field names. Existing SKUs cause a rejected import.</p>`,async fd=>{const result=await post(`/api/imports/supplier-api?supplier_id=${numeric(fd,"supplier_id")}&location_id=${numeric(fd,"location_id")}`);return `${result.imported} products imported through a real HTTP call to the demo supplier.`;},"This is a separate local API, not a real supplier connection.","Fetch & import catalog");
  }
  if(name==="staff")return openModal("Create staff account",`<div class="form-grid">${field("Display name","display_name","text","","required maxlength=80")}${field("Username","username","text","","required minlength=3 maxlength=40 pattern='[A-Za-z0-9_.-]+' autocomplete=off")}${field("Password","password","password","","required minlength=12 maxlength=128 autocomplete=new-password",true)}${select("Staff role","role",'<option value="cashier">Cashier · stock and sales</option><option value="manager">Manager · shop operations</option><option value="owner">Owner · full access</option>')}</div>`,async fd=>{await post("/api/staff",Object.fromEntries(fd));return "Staff account created.";},"Use a unique password with at least 12 characters.");
  if(name==="deactivate")return openModal("Deactivate staff account",'<p>This staff member will be signed out and cannot sign in again. Their past activity is preserved.</p><label class="checkbox-label"><input type="checkbox" required>I want to deactivate this account.</label>',async()=>{await post(`/api/staff/${id}/deactivate`);return "Staff account deactivated and sessions revoked.";},"","Deactivate");
  if(name==="backup"){const result=await post("/api/backups");await render();message(`Backup created: ${result.name}`);return;}
  if(name==="restore")return openModal("Restore a database backup",`<p class="form-error">This replaces current products, sales, staff, and other shop records with the selected backup. A safety copy of the current database is made first.</p><p class="backup-name">${esc(id)}</p>${field("Type RESTORE to confirm","confirmation","text","","required pattern=RESTORE autocomplete=off")}${field("Your current owner password","password","password","","required maxlength=128 autocomplete=current-password")}`,async fd=>{await post(`/api/backups/${encodeURIComponent(id)}/restore`,Object.fromEntries(fd));return {signout:true};},"After restoring, sign in using an account and password contained in the backup.","Restore & sign out");
  if(name==="previous"||name==="next"){pageOffset=Math.max(0,pageOffset+(name==="next"?20:-20));await render();}
}
document.addEventListener("click",event=>{
  if(event.target.closest("[data-close]")){if(!busy)$("#work-dialog").close();return;}
  const remove=event.target.closest(".remove-line");if(remove&&$$(".order-line").length>1){const kind=remove.closest(".order-line").querySelector('[name=cost]')?"purchase":"sale";remove.closest(".order-line").remove();updateLineTotal(kind);return;}
  const target=event.target.closest("[data-action]");if(target&&!busy&&user)action(target.dataset.action,target.dataset.id).catch(exc=>message(exc.message,true));
});
$("#work-dialog").addEventListener("cancel",event=>{if(busy)event.preventDefault();});
window.addEventListener("hashchange",()=>{pageOffset=0;filter="";if(user)render();});
(async()=>{try{const state=await api("/api/auth/status");if(state.needs_setup){showAuth(true);return;}try{user=await api("/api/auth/me");await enterWorkspace();}catch{showAuth(false);}}catch(exc){$("#auth-description").textContent=`Cannot connect to StockDesk: ${exc.message}`;}})();
