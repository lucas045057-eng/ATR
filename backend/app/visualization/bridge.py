import json

PROTOCOL='atr-visualization-1'


def bridge_message(value,snapshot_id,nonce,allowed_ids):
    return (isinstance(value,dict) and set(value)=={'protocol','snapshot_id','nonce','type','id'}
            and value['protocol']==PROTOCOL and value['snapshot_id']==snapshot_id and value['nonce']==nonce
            and value['type']=='source' and value['id'] in allowed_ids)


def embed_bridge(html,snapshot_id,nonce):
    configuration=json.dumps({'protocol':PROTOCOL,'snapshot_id':snapshot_id,'nonce':nonce},ensure_ascii=True).replace('<','\\u003c')
    script='''<script>(()=>{const config=CONFIG;document.addEventListener('click',event=>{
      const node=event.target.closest('[data-node-id],[data-edge-id]');if(!node)return;
      window.parent.postMessage({...config,type:'source',id:node.dataset.nodeId||node.dataset.edgeId},'*');});})();</script>'''.replace('CONFIG',configuration)
    policy="<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'\">"
    return html.replace('<head>','<head>'+policy,1).replace('</body>',script+'</body>',1)
