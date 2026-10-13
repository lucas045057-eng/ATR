import React,{useEffect,useRef,useState} from 'react';
import {api,Row} from './api';
import {Theme} from './theme';

export function StructureView({businessId,theme}:{businessId:string;theme:Theme}) {
  const [view,setView]=useState('structure');
  const [flow,setFlow]=useState('');
  const [diagram,setDiagram]=useState<Row|null>(null);
  const [error,setError]=useState('');
  const [loading,setLoading]=useState(false);
  const [selected,setSelected]=useState<Row[]>([]);
  const [code,setCode]=useState<Row|null>(null);
  const [image,setImage]=useState('');
  const [previewRef,setPreviewRef]=useState<Row|null>(null);
  const [exportFile,setExportFile]=useState<Row|null>(null);
  const [refreshVersion,setRefreshVersion]=useState(0);
  const profileVersion=useRef<number|null>(null);
  const graphSnapshot=useRef<string|null>(null);
  const frame=useRef<HTMLIFrameElement>(null);
  const sourceController=useRef<AbortController|null>(null);
  useEffect(()=>{
    let active=true;const controller=new AbortController();setLoading(true);setError('');setDiagram(null);setSelected([]);setExportFile(null);
    sourceController.current?.abort();setCode(null);setImage('');setPreviewRef(null);
    api(`/businesses/${businessId}/visualization?view=${view}&theme=${theme}${profileVersion.current?'&profile_version='+profileVersion.current:''}${graphSnapshot.current?'&graph_snapshot_id='+encodeURIComponent(graphSnapshot.current):''}${view==='flow'&&flow?'&flow_id='+encodeURIComponent(flow):''}`,{signal:controller.signal})
      .then(value=>{if(active){profileVersion.current=value.profile_version;graphSnapshot.current=value.graph_snapshot_id;setDiagram(value);}}).catch(reason=>{if(active)setError(reason.message);})
      .finally(()=>{if(active)setLoading(false);});
    return()=>{active=false;controller.abort();sourceController.current?.abort();};
  },[businessId,view,flow,theme,refreshVersion]);
  useEffect(()=>{
    let active=true;
    const receive=async(event:MessageEvent)=>{
      const value=event.data;
      if(event.source!==frame.current?.contentWindow||event.origin!=='null'||!diagram||!value||typeof value!=='object')return;
      if(value.protocol!=='atr-visualization-1'||value.snapshot_id!==diagram.snapshot_id||value.nonce!==diagram.nonce)return;
      if(value.type==='export'){
        const blob=value.blob;
        if(!(blob instanceof Blob)||blob.size>20*1024*1024||blob.size<8)return;
        const extensions:Row={'image/png':'png','image/jpeg':'jpg','image/webp':'webp','image/svg+xml':'svg'};
        const mime=blob.type.split(';')[0];if(!extensions[mime])return;
        const bytes=new Uint8Array(await blob.arrayBuffer());if(!active)return;
        if(mime==='image/png'&&bytes.slice(0,8).join(',')!=='137,80,78,71,13,10,26,10')return;
        if(mime==='image/svg+xml'){
          const document=new DOMParser().parseFromString(new TextDecoder().decode(bytes),'image/svg+xml');
          if(document.documentElement.localName!=='svg'||document.querySelector('parsererror,script,foreignObject'))return;
        }
        const name=`ATR-${diagram.snapshot_id.slice(0,16)}-${diagram.view}-${diagram.theme}.${extensions[mime]}`;
        const url=URL.createObjectURL(blob);setExportFile({name,url,bytes:blob.size,format:extensions[mime],snapshot_id:diagram.snapshot_id});
        const anchor=document.createElement('a');anchor.href=url;anchor.download=name;anchor.click();
        return;
      }
      if(value.type!=='source')return;
      if(!Object.prototype.hasOwnProperty.call(diagram.sources,value.id))return;
      sourceController.current?.abort();
      setSelected(diagram.sources[value.id]);setCode(null);setImage('');
    };
    window.addEventListener('message',receive);return()=>{active=false;window.removeEventListener('message',receive);};
  },[diagram]);
  useEffect(()=>()=>{if(image)URL.revokeObjectURL(image);},[image]);
  useEffect(()=>()=>{if(exportFile)URL.revokeObjectURL(exportFile.url);},[exportFile]);
  const openSource=async(reference:Row)=>{
    sourceController.current?.abort();const controller=new AbortController();sourceController.current=controller;
    setError('');setCode(null);setImage('');setPreviewRef(reference);
    try {
      if(reference.observation_id){
        const value=await api(`/businesses/${businessId}/observations/${reference.observation_id}`,{signal:controller.signal});
        if(!controller.signal.aborted)setCode(value);
      } else if(reference.page_number){
        const token=sessionStorage.getItem('atr_operator_token')||'';
        const response=await fetch(`/api/v1/businesses/${businessId}/files/${reference.file_id}/page?profile_version=${diagram?.profile_version}&page_number=${reference.page_number}`,{signal:controller.signal,headers:{Authorization:`Bearer ${token}`}});
        if(!response.ok)throw new Error('该页原件暂不可查看，请核对页码、资料版本和暂停原因');
        const value=await response.blob();if(!controller.signal.aborted)setImage(URL.createObjectURL(value));
      } else if(reference.file_id){
        const value=await api(`/businesses/${businessId}/code/context`,{signal:controller.signal,method:'POST',body:JSON.stringify({file_id:reference.file_id,profile_version:diagram?.profile_version,start_line:reference.start_line||reference.line||1,max_lines:80})});
        if(!controller.signal.aborted)setCode(value);
      }
    } catch(reason){if(!controller.signal.aborted)setError((reason as Error).message);}
  };
  return <section className="panel structure-panel">
    <h2>业务结构图</h2><p>查看组件与关系，点击图中的节点或箭头追溯来源。资料描述和静态路径仍需运行证据核验。</p>
    <div className="row"><button className="btn" onClick={()=>{setView('structure');setFlow('');}}>业务结构</button><button className="btn" onClick={()=>setView('flow')}>源码流程</button>
      <button className="btn" disabled={loading} onClick={()=>{profileVersion.current=null;graphSnapshot.current=null;setFlow('');setRefreshVersion(value=>value+1);}}>更新到最新资料版本</button>
      {view==='flow'&&(diagram?.flows?.length||0)>0&&<select aria-label="选择源码流程" value={flow} onChange={event=>setFlow(event.target.value)}><option value="">全部源码入口</option>{(diagram?.flows||[]).map((item:Row)=><option key={item.flow_id} value={item.flow_id}>{item.kind==='startup'?'启动':'请求或后台'} · {item.entry}</option>)}</select>}</div>
    {loading&&<p role="status">正在生成同一资料版本的图形，最多等待 30 秒…</p>}
    {error&&<p role="alert" className="alert error">{error}</p>}
    {diagram&&<><p>资料版本 v{diagram.profile_version} · {diagram.node_count} 个节点 / {diagram.edge_count} 条关系 · 遗漏 {diagram.omitted_nodes} 个节点、{diagram.omitted_edges} 条关系</p>
      <iframe ref={frame} title="可交互的业务结构与流程图" sandbox="allow-scripts" srcDoc={diagram.html} style={{width:'100%',height:760,border:0}}/>
      {exportFile&&<p role="status">✓ {exportFile.format.toUpperCase()} 图形已生成 · {exportFile.bytes} 字节 · 当前资料版本 v{diagram.profile_version} <a href={exportFile.url} download={exportFile.name}>保存已生成的图形文件</a></p>}
      {diagram.unknowns.length>0&&<details><summary>待核验信息 · {diagram.unknowns.length} 项</summary>{diagram.unknowns.map((value:string,index:number)=><p key={index}>{value}</p>)}</details>}
      <div className="source-panel"><h3>图中来源</h3>{selected.length===0?<p>点击一个节点或箭头；缺少来源时不能当作已验证事实。</p>:selected.map((ref:Row,index:number)=><div key={index}><p>{ref.source_type} · {ref.path||ref.archive_path} · {ref.page_number?'第 '+ref.page_number+' 页':'行 '+(ref.start_line||ref.line||1)} · {ref.content_digest?.slice(0,12)}</p><button className="btn small" onClick={()=>openSource(ref)}>查看该版本原件</button>{ref.region&&<small>来源区域：{JSON.stringify(ref.region)}</small>}</div>)}</div>
      {code&&<pre>{code.text||code.content||JSON.stringify(code,null,2)}</pre>}{image&&<div style={{position:'relative',width:'100%'}}><img src={image} alt="该版本资料页" style={{display:'block',width:'100%'}}/>{previewRef?.region?.length===4&&<div aria-label="引用事实所在区域" style={{position:'absolute',pointerEvents:'none',border:'3px solid #8b5cf6',background:'rgba(139,92,246,.12)',left:previewRef.region[0]*100+'%',top:previewRef.region[1]*100+'%',width:(previewRef.region[2]-previewRef.region[0])*100+'%',height:(previewRef.region[3]-previewRef.region[1])*100+'%'}}/>}</div>}
    </>}
  </section>;
}
