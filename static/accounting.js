"use strict";
// Invoice integration screens use the same forms, API helper, and styling as the shop.
let invoiceConnection=null, invoiceOffset=0;
const invoiceStatus=status=>({completed:"Completed",completed_with_errors:"Needs correction",partial_failure:"Stopped after saved pages",failed:"Failed",running:"Running",interrupted:"Interrupted"})[status]||status;
const invoiceFailure=status=>["failed","partial_failure","interrupted"].includes(status);

async function accountingPanel(){
  const [connection,runs,ledger]=await Promise.all([api("/api/accounting/connection"),api("/api/accounting/runs"),api(`/api/accounting/invoices?offset=${invoiceOffset}&limit=20`)]);
  invoiceConnection=connection;
  const last=runs[0];
  const actions=`<div class="actions invoice-actions">${user.role==="owner"?button("Connection settings","invoice-settings"):""}${connection?button("Supplier mappings","invoice-mappings"):""}${connection&&connection.key_configured?button("Sync invoices","invoice-sync","",true):""}</div>`;
  const intro=connection?`<p><strong>${esc(connection.name)}</strong> · ${connection.key_configured?"API key configured":"API key needs configuring"} · Separate fictional accounting service</p><p class="helper">Invoices are matched to suppliers and purchase orders. Importing an invoice records billing; receiving a purchase remains a separate action.</p><div class="actions">${button("Demo lab","invoice-demo")}<a href="http://127.0.0.1:8703/docs" target="_blank" rel="noopener" class="text-button">Mock API documentation ↗</a></div>`:`<p>Collect purchase invoices directly from the mock accounting application. Owners can connect using the locally configured demo key, then managers can sync invoices.</p>`;
  const stats=last?metrics([["Imported in latest sync",last.imported,`${last.pages} pages processed`],["Already imported",last.duplicates,"Safe repeats; no new copy created"],["Invoice errors",last.errors,"Review the run for details"],["Invoices in ledger",ledger.total,"Across completed and partially saved syncs"]]):"";
  const history=runs.length?table(["SYNC / STARTED","RESULT","IMPORTED / EXISTING / ERRORS","REQUESTS",""],runs.map(run=>cells([
    `#${run.id}<small class="block muted">${stamp(run.started_at)}</small>`,badge(invoiceStatus(run.status),invoiceFailure(run.status)?"out":run.errors?"low":""),`${run.imported} / ${run.duplicates} / ${run.errors}`,`${run.attempt_count} attempts`,button("Details","invoice-run",run.id)
  ]))):empty("No invoice sync yet","Configure the connection, check supplier mappings, then click Sync invoices.");
  const invoices=ledger.items.length?table(["INVOICE / DATE","SUPPLIER","PURCHASE ORDER","TOTAL",""],ledger.items.map(item=>cells([
    `${esc(item.invoice_number)}<small class="block muted">${day(item.invoice_date)}</small>`,esc(item.supplier),`#${item.purchase_id}`,money(item.total_paise),button("View","invoice-view",item.id)
  ]))):empty("No invoices imported","Valid invoices will appear here after syncing.");
  const pages=ledger.total?`<div class="count-caption pagination"><span>${invoiceOffset+1}–${Math.min(invoiceOffset+20,ledger.total)} of ${ledger.total} invoices</span><div>${invoiceOffset?button("← Previous","invoice-previous"):""}${invoiceOffset+20<ledger.total?button("Next →","invoice-next"):""}</div></div>`:"";
  return panel("Accounting invoice sync","Authenticated API calls, field conversion, validation, and safe repeats.",`<div class="panel-body">${intro}</div>${stats}${last?`<div class="invoice-last-result ${invoiceFailure(last.status)?"invoice-error":""}"><strong>${invoiceStatus(last.status)}</strong> · ${esc(last.message)}</div>`:""}`,actions)+panel("Invoice sync history","Latest 20 runs. Saved pages remain available after a later page fails.",history)+panel("Imported purchase invoices","Original source IDs and amounts are retained. Changed invoices require review.",invoices+pages);
}

async function accountingAction(name,id){
  if(name==="invoice-settings"){
    const configured=invoiceConnection?.key_configured;
    openModal("Accounting connection",`${field("Connection name","name","text",invoiceConnection?.name||"Demo accounting","required maxlength=80")}<p class="helper">Mock accounting endpoint: http://127.0.0.1:8703 · API keys are sent in a request header and kept out of logs and database backups.</p><label class="checkbox-label"><input type="checkbox" name="use_demo_key" ${configured?"":"checked"}>Use the locally configured demo key</label>${field("API key (leave blank to keep the current key)","api_key","password","","minlength=8 maxlength=256 autocomplete=off")}`,async fd=>{
      const result=await post("/api/accounting/connection",{name:fd.get("name"),use_demo_key:fd.has("use_demo_key"),api_key:fd.get("api_key")||null});
      return {message:result.source_error?`Connection saved. ${result.source_error}`:"Accounting connected. Matching supplier names were mapped automatically; review Supplier mappings before syncing.",error:!!result.source_error};
    },"This project connects to a fictional local accounting service.","Save connection");
    const toggle=$("#work-form [name=use_demo_key]"),key=$("#work-form [name=api_key]");key.disabled=toggle.checked;toggle.onchange=()=>{key.disabled=toggle.checked;};
    return;
  }
  if(name==="invoice-mappings"){
    const connection=await api("/api/accounting/connection");
    if(!connection.mappings.length){message("No supplier list loaded. Refresh it after the accounting service is available.",true);}
    openModal("Accounting supplier mappings",`${connection.mappings.map(item=>`<label class="invoice-map-label">${esc(item.source_name)}<small>${esc(item.source_code)}</small><select class="invoice-map" data-code="${esc(item.source_code)}">${supplierOptions(item.supplier_id,true)}</select></label>`).join("")}<button type="button" class="text-button" id="invoice-refresh-suppliers">Refresh source supplier list</button>`,async(fd,form)=>{
      for(const input of form.querySelectorAll(".invoice-map"))await post("/api/accounting/suppliers/map",{source_code:input.dataset.code,supplier_id:input.value?Number(input.value):null});
      return "Supplier mappings saved. Retry sync to recheck previously rejected invoices.";
    },"Match each accounting supplier to the corresponding supplier in your shop.","Save mappings");
    $("#invoice-refresh-suppliers").onclick=async()=>{try{await post("/api/accounting/suppliers/refresh");$("#work-dialog").close();await accountingAction("invoice-mappings");}catch(exc){const error=$("#work-form .form-error");error.textContent=exc.message;error.hidden=false;}};
    return;
  }
  if(name==="invoice-sync"){
    if(!invoiceConnection?.key_configured){message("Configure the accounting connection first.",true);return;}
    openModal("Sync purchase invoices",`<p>Fetch invoices in pages of 100, validate their dates and amounts, and match them to suppliers and purchase orders.</p><p class="helper">Previously imported invoices are skipped. Changed invoices are flagged. If a later page fails, earlier saved pages stay in the ledger. Retrying is safe.</p>`,async()=>{
      const submit=$("#work-form [type=submit]");submit.textContent="Syncing invoices…";
      const run=await post("/api/accounting/sync");invoiceOffset=0;
      return {message:`Sync #${run.id}: ${invoiceStatus(run.status)}. ${run.imported} imported, ${run.duplicates} already existed, ${run.errors} need correction. ${run.message}`,error:invoiceFailure(run.status)};
    },"The source is a separate mock service with fictional invoices. Stock and sales profit are not changed by invoice import.","Start sync");
    return;
  }
  if(name==="invoice-run"){
    const run=await api(`/api/accounting/runs/${id}`);
    const errors=run.error_details.length?table(["PAGE / SOURCE","ERROR","EXPLANATION"],run.error_details.map(item=>cells([`${item.page}<small class="block muted">${esc(item.source_id)}</small>`,esc(item.code),esc(item.message)]))):empty("No invoice row errors","Connection or pagination failures are described in the run message and request history.");
    const attempts=table(["PAGE / ATTEMPT","HTTP","RESULT","WAIT"],run.attempts.map(item=>cells([`${item.page} / ${item.attempt}`,item.status_code??"—",`${esc(item.code)}<small class="block muted">${esc(item.message)}</small>`,`${item.retry_delay} s`])));
    openModal(`Invoice sync #${id}`,`<p><strong>${invoiceStatus(run.status)}</strong> · ${esc(run.message)}</p><p class="helper">${run.imported} imported · ${run.duplicates} already existed · ${run.errors} errors · ${run.pages} pages processed</p>${run.errors?`<a class="text-button" href="/api/accounting/runs/${id}/errors.csv">Download all invoice errors</a>`:""}<h3>Invoice validation</h3>${errors}<h3 class="mapping-heading">Request and retry history</h3>${attempts}<details class="mapping-heading"><summary>Field and supplier mapping used</summary><pre>${esc(JSON.stringify(JSON.parse(run.mapping_json),null,2))}</pre></details>`,async()=>"", "Latest 100 row errors and 200 request attempts shown.","Done");
    return;
  }
  if(name==="invoice-view"){
    const invoice=await api(`/api/accounting/invoices/${id}`);
    openModal(esc(invoice.invoice_number),`<p>${esc(invoice.supplier)} · Purchase #${invoice.purchase_id} · ${day(invoice.invoice_date)}</p>${table(["PRODUCT","QUANTITY","UNIT COST","LINE TOTAL"],invoice.items.map(item=>cells([`${esc(item.name)}<small class="block muted">${esc(item.sku)}</small>`,item.quantity,money(item.unit_cost_paise),money(item.line_total_paise)])))}<div class="sale-total"><span>Invoice total</span><strong>${money(invoice.total_paise)}</strong></div><p class="helper">Source ID: ${esc(invoice.source_id)}<br>Imported in sync #${invoice.run_id}. The original record is immutable.</p>`,async()=>"","Linked to the purchase order; this does not receive stock or process a payment.","Done");
    return;
  }
  if(name==="invoice-demo"){
    const state=await api("/api/accounting/demo/fault");
    const modes={normal:"Normal service",temporary_outage:"Temporary outage: retry once, then succeed",rate_limit:"Page two rate limit: wait one second, then retry",page_two_outage:"Page two outage: keep failing until reset",slow_page:"Slow page two: demonstrate timeouts",invalid_data:"Three invalid invoices: total, supplier, date",changed_invoice:"Changed first invoice: preserve the imported original"};
    openModal("Accounting demo lab",select("Simulate scenario","mode",Object.entries(modes).map(([value,label])=>`<option value="${value}" ${state.mode===value?"selected":""}>${esc(label)}</option>`).join("")),async fd=>{await post("/api/accounting/demo/fault",{mode:fd.get("mode")});return "Demo scenario set. Click Sync invoices to observe it. Select Normal service when finished.";},"These controls affect only the fictional accounting API. For a page-two outage, the first 100 can be saved before later requests fail. After restoring Normal service, repeat sync safely.","Set scenario");
    return;
  }
  if(name==="invoice-next"||name==="invoice-previous"){
    invoiceOffset=Math.max(0,invoiceOffset+(name==="invoice-next"?20:-20));await render();
  }
}
