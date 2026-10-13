import React, {useEffect, useRef, useState} from 'react';
import {api, Row} from './api';

export function ModelSettings({onClose}:{onClose:()=>void}) {
  const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{const current=dialog.current;current?.showModal();return()=>current?.close();},[]);
  const [configuration,setConfiguration]=useState<Row|null>(null);
  const [savedConfiguration,setSavedConfiguration]=useState('');
  const [key,setKey]=useState('');
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [busy,setBusy]=useState(false);
  const [job,setJob]=useState(sessionStorage.getItem('atr_active_model_probe_job')||'');
  const fingerprint=(value:Row)=>JSON.stringify([value.provider,value.base_url,value.model,value.wire_api,value.external_analysis_enabled]);
  useEffect(()=>{let active=true;api('/model/configuration').then(value=>{if(active){setConfiguration(value);setSavedConfiguration(fingerprint(value));}}).catch(reason=>{if(active)setError(reason.message);});return()=>{active=false;};},[]);
  useEffect(()=>{
    if(!job)return;
    setBusy(true);let active=true;let timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{
      try {
        const value=await api(`/jobs/${job}`);if(!active)return;
        setError('');
        if(['queued','running'].includes(value.status)){timer=setTimeout(poll,1500);return;}
        setJob('');setBusy(false);sessionStorage.removeItem('atr_active_model_probe_job');
        if(value.status==='completed')setNotice('✓ 实际服务响应已通过检测，可查看下方能力状态。');
        else setError(value.error||'检测失败，原配置和调用记录已保留。');
        const current=await api('/model/configuration');if(active){setConfiguration(current);setSavedConfiguration(fingerprint(current));}
      } catch(reason){if(active){setError(`连接中断，检测状态暂时未知；恢复后继续读取原任务。${(reason as Error).message}`);timer=setTimeout(poll,1500);}}
    };
    void poll();return()=>{active=false;clearTimeout(timer);};
  },[job]);
  const save=async(event:React.FormEvent)=>{
    event.preventDefault();if(!configuration)return;setBusy(true);setError('');setNotice('');
    try {
      const current=await api('/model/configuration',{method:'PUT',body:JSON.stringify({provider:configuration.provider,base_url:configuration.base_url,model:configuration.model,wire_api:configuration.wire_api,external_analysis_enabled:configuration.external_analysis_enabled,api_key:key})});
      setConfiguration(current);setSavedConfiguration(fingerprint(current));setKey('');setNotice('✓ 模型设置已加密保存，原环境配置文件保留。请检测所选服务的实际能力。');
    } catch(reason){setError((reason as Error).message);}finally{setBusy(false);}
  };
  const probe=async(kind:string)=>{
    setBusy(true);setError('');setNotice('');
    try {
      const result=await api(`/model/probe-${kind}`,{method:'POST'});
      sessionStorage.setItem('atr_active_model_probe_job',result.job_id);setJob(result.job_id);
    } catch(reason){setError((reason as Error).message);setBusy(false);}
  };
  const capabilities=configuration?.capabilities||{};
  const hasUnsavedChanges=!!key||!!configuration&&fingerprint(configuration)!==savedConfiguration;
  return <dialog ref={dialog} aria-labelledby="model-settings-heading" className="settings-modal" onCancel={event=>{event.preventDefault();onClose();}}>
    <div className="panel model-settings"><div className="panel-title"><h2 id="model-settings-heading">模型设置与连通性</h2><button className="btn small" onClick={onClose}>关闭设置</button></div>
      <p>填写你选用的模型服务。保存后只调用该服务；检测使用生成的文字和图片，每项最多等待 45 秒，可能产生模型费用。</p>
      {error&&<p role="alert" className="alert error">! {error}</p>}{notice&&<p role="status" className="alert success">{notice}</p>}
      {!configuration?<p>正在读取当前设置…</p>:<form className="form" onSubmit={save}>
        <fieldset className="model-fields" disabled={busy}>
        <label>服务类型<input value={configuration.provider} readOnly/></label>
        <label>模型服务地址<input type="url" required maxLength={2048} value={configuration.base_url} onChange={event=>setConfiguration({...configuration,base_url:event.target.value})}/></label>
        <label>模型名称<input required maxLength={200} value={configuration.model} onChange={event=>setConfiguration({...configuration,model:event.target.value})}/></label>
        <label>服务密钥<input type="password" autoComplete="off" maxLength={8192} required={!configuration.api_key_configured} value={key} onChange={event=>setKey(event.target.value)} placeholder={configuration.api_key_configured?'已配置；留空保留当前服务密钥':'填写所选服务密钥'}/></label>
        <label>接口协议<select value={configuration.wire_api} onChange={event=>setConfiguration({...configuration,wire_api:event.target.value})}><option value="chat_completions">Chat Completions</option><option value="responses">Responses</option></select></label>
        <label className="check-label"><input type="checkbox" checked={configuration.external_analysis_enabled} onChange={event=>setConfiguration({...configuration,external_analysis_enabled:event.target.checked})}/>启用所选服务分析业务资料与排查证据</label>
        <p>密钥状态：{configuration.api_key_configured?'已配置，页面不返回密钥':'未配置'} · 来源：{configuration.source==='existing_environment'?'原有环境配置':'操作员加密设置'}</p>
        <button className="btn primary" disabled={busy}>保存模型设置</button>
        </fieldset>
      </form>}
      {hasUnsavedChanges&&<p role="status">设置尚未保存；请先保存修改，再检测该服务。</p>}
      <div className="business-actions"><button className="btn" disabled={busy||hasUnsavedChanges||!capabilities.configured||!capabilities.external_analysis_enabled} onClick={()=>void probe('text')}>检测文字与结构化 JSON</button><button className="btn" disabled={busy||hasUnsavedChanges||!capabilities.configured||!capabilities.external_analysis_enabled} onClick={()=>void probe('vision')}>检测图片理解能力</button></div>
      <p>已保存模型 {capabilities.model||'未配置'} · 文字与 JSON：{capabilities.text_verified&&capabilities.json_verified?'✓ 已经实际响应验证':'？尚未通过实际验证'} · 图片：{capabilities.vision_verified?'✓ 已经实际图片验证':capabilities.vision_declared?'△ 原配置声明支持，仍需实际验证':'？尚未通过实际图片验证'}</p>
      {busy&&<p role="status">正在保存或检测；关闭页面后后台任务及调用记录仍会保留。</p>}
    </div>
  </dialog>;
}
