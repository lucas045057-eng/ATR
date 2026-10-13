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
    export_bridge='''<script>(()=>{const config=CONFIG;const blobs=new Map();
      const create=URL.createObjectURL.bind(URL),revoke=URL.revokeObjectURL.bind(URL);
      URL.createObjectURL=value=>{const url=create(value);if(value instanceof Blob)blobs.set(url,value);return url;};
      URL.revokeObjectURL=url=>{blobs.delete(url);revoke(url);};
      document.addEventListener('click',event=>{const anchor=event.target.closest('a[download]');
        if(!anchor)return;event.preventDefault();const blob=blobs.get(anchor.href);
        if(!blob||blob.size>20*1024*1024||!['image/png','image/jpeg','image/webp','image/svg+xml'].includes(blob.type.split(';')[0]))return;
        window.parent.postMessage({...config,type:'export',blob},'*');},true);
    })();</script>'''.replace('CONFIG',configuration)
    # The parent owns theme state. Keep the fixed vendor bytes intact and use its public theme API.
    theme_script='''<script>(()=>{const theme=THEME;const sync=()=>{
      if(document.documentElement.dataset.theme!==theme){
        if(window.Archify?.theme)window.Archify.theme.toggle();
        else document.documentElement.dataset.theme=theme;
      }
      const button=document.getElementById('btn-theme');if(button)button.style.display='none';
    };sync();document.addEventListener('DOMContentLoaded',sync);
    new MutationObserver(sync).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});})();</script>'''.replace('THEME',json.dumps(theme))
    return html.replace('<head>','<head>'+policy+export_bridge+theme_script,1).replace('</body>',script+theme_script+'</body>',1)
