import json

PROTOCOL='atr-visualization-1'


def bridge_message(value,snapshot_id,nonce,allowed_ids):
    return (isinstance(value,dict) and set(value)=={'protocol','snapshot_id','nonce','type','id'}
            and value['protocol']==PROTOCOL and value['snapshot_id']==snapshot_id and value['nonce']==nonce
            and value['type']=='source' and value['id'] in allowed_ids)


def embed_bridge(html,snapshot_id,nonce,theme='dark'):
    if theme not in {'light','dark'}:
        raise ValueError('未知图主题')
    configuration=json.dumps({'protocol':PROTOCOL,'snapshot_id':snapshot_id,'nonce':nonce},ensure_ascii=True).replace('<','\\u003c')
    script='''<script>(()=>{const config=CONFIG;document.addEventListener('click',event=>{
      const node=event.target.closest('[data-node-id],[data-edge-id]');if(!node)return;
      window.parent.postMessage({...config,type:'source',id:node.dataset.nodeId||node.dataset.edgeId},'*');});})();</script>'''.replace('CONFIG',configuration)
    policy="<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'\">"
    # The parent owns theme state. Keep the fixed vendor bytes intact and use its public theme API.
    theme_script='''<script>(()=>{const theme=THEME;const sync=()=>{
      if(document.documentElement.dataset.theme!==theme){
        if(window.Archify?.theme)window.Archify.theme.toggle();
        else document.documentElement.dataset.theme=theme;
      }
      const button=document.getElementById('btn-theme');if(button)button.hidden=true;
    };sync();document.addEventListener('DOMContentLoaded',sync);
    new MutationObserver(sync).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});})();</script>'''.replace('THEME',json.dumps(theme))
    return html.replace('<head>','<head>'+policy+theme_script,1).replace('</body>',script+theme_script+'</body>',1)
