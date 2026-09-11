#!/usr/bin/env node
/* Editorial-only crop, chapter labels and cuts. No task-state animation. */
const fs=require('fs'),path=require('path'),{spawn}=require('child_process');
const {createCanvas,GlobalFonts,loadImage}=require('@napi-rs/canvas');
const crypto=require('crypto');
const flags=Object.fromEntries(process.argv.slice(2).map((v,i,a)=>v.startsWith('--')?[v.slice(2),a[i+1]]:[]).filter(x=>x.length));
if(!flags.input) throw Error('--input recording directory is required');
const dir=path.resolve(flags.input),meta=JSON.parse(fs.readFileSync(path.join(dir,'recording.json')));
const verify=JSON.parse(fs.readFileSync(path.join(dir,'verification.json')));
const ffmpeg=flags.ffmpeg||'ffmpeg',ffprobe=flags.ffprobe||'ffprobe';
const font=flags.font||'/System/Library/Fonts/Hiragino Sans GB.ttc';
if(!GlobalFonts.registerFromPath(font,'ShowcaseCN')) throw Error('Chinese font did not load: '+font);
const work=path.join(dir,'edit');fs.mkdirSync(work,{recursive:true});
function run(exe,args){return new Promise((resolve,reject)=>{
 const p=spawn(exe,args,{stdio:['ignore','pipe','pipe']});let out='',err='';
 p.stdout.on('data',d=>out+=d);p.stderr.on('data',d=>err+=d);
 p.on('close',code=>code===0?resolve(out):reject(Error(exe+' failed '+code+'\n'+err)));
});}
const sha=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
function caption(file,title,subtitle,extra='原生 UI / SSE 实录 · 手动编排 · 本地 fixture · 非业务验收'){
 const canvas=createCanvas(1920,1080),c=canvas.getContext('2d');
 c.fillStyle='#080b10';c.fillRect(0,0,1920,96);c.fillRect(0,988,1920,92);
 c.fillStyle='#37d6d0';c.font='14px ShowcaseCN';c.fillText('QINGTIAN   /   REAL ENGINE SHOWCASE',36,23);
 c.fillStyle='#f2f6fa';c.font='bold 33px ShowcaseCN';c.fillText(title,36,69);
 c.strokeStyle='#2a6566';c.lineWidth=1;c.strokeRect(1435,20,449,52);
 c.fillStyle='#80e8df';c.font='21px ShowcaseCN';c.fillText('真实引擎 · 合成示例 · 非生产执行',1451,54);
 c.fillStyle='#f2f6fa';c.font='24px ShowcaseCN';
 if(c.measureText(subtitle).width>1848) throw Error('Caption overflow: '+subtitle);
 c.fillText(subtitle,36,1024);
 c.fillStyle='#91a4b6';c.font='16px ShowcaseCN';c.fillText(extra,36,1062);
 fs.writeFileSync(file,canvas.toBuffer('image/png'));
}
function cropAt(t){
 const camera=meta.cameras.find(c=>t>=c.start&&t<c.end);
 if(!camera)return null;
 const crop={...camera.crop};
 crop.y=t>(camera.start+camera.end)/2?camera.crop_bottom:camera.crop_top;
 return crop;
}
async function prepareCrops(){
 for(const camera of meta.cameras){
  let top,bottom;
  if(camera.dialog_bounds){top=camera.dialog_bounds.y;bottom=top+camera.dialog_bounds.height;}
  else{
   // Older raw recordings did not save the read-only dialog bounding box.
   // Recover its fixed 720px dialog border from the actual recorded pixels.
   const file=path.join(work,'camera-'+camera.alias+'.png');
   await run(ffmpeg,['-hide_banner','-loglevel','error','-y','-ss',String((camera.start+camera.end)/2),'-i',meta.raw,'-frames:v','1',file]);
   const im=await loadImage(file),canvas=createCanvas(2240,1040),c=canvas.getContext('2d');c.drawImage(im,0,0);
   const d=c.getImageData(760,0,1,1040).data,rows=[];
   for(let y=0;y<1040;y++){const [r,g,b]=d.slice(y*4,y*4+3);if(r>45&&r<100&&g>45&&g<100&&b>45&&b<110)rows.push(y);}
   if(rows.length<150)throw Error('Cannot safely locate actual dialog border: '+camera.alias);
   top=rows[0]-12;bottom=rows.at(-1)+12;
  }
  const clamp=y=>Math.round(Math.max(0,Math.min(510,y)));
  if(bottom-top<=510)camera.crop_top=camera.crop_bottom=clamp((top+bottom-530)/2);
  else {camera.crop_top=clamp(top-20);camera.crop_bottom=clamp(bottom+20-530);}
  camera.observed_dialog_vertical_bounds={top,bottom};
 }
 fs.writeFileSync(path.join(work,'camera-crops.json'),JSON.stringify(meta.cameras,null,2));
}
async function segment(index,start,end,title,subtitle,crop,technical=false){
 const label=String(index).padStart(3,'0'),overlay=path.join(work,label+'-caption.png'),file=path.join(work,label+'.mp4');
 caption(overlay,title,subtitle,technical?'技术详情的模型栏是产品默认配置；本片未调用任何模型，也没有创建真实 Worker 运行。':undefined);
 const camera=crop?`crop=${crop.w}:${crop.h}:${crop.x}:${crop.y},`:'';
 const filter=`[0:v]${camera}setpts=PTS-STARTPTS,fps=30,scale=1920:892:flags=lanczos,pad=1920:1080:0:96:color=0x080b10[base];[base][1:v]overlay=0:0:format=auto,format=yuv420p,setsar=1[out]`;
 await run(ffmpeg,['-hide_banner','-loglevel','warning','-y','-ss',start.toFixed(3),'-t',(end-start+.1).toFixed(3),'-i',meta.raw,'-i',overlay,
  '-filter_complex',filter,'-map','[out]','-an','-r','30','-c:v','libx264','-preset','veryfast','-crf','19','-threads','4','-frames:v',String(Math.round((end-start)*30)),file]);
 return file;
}
function timecode(sec){const n=Math.floor(sec);return `${String(Math.floor(n/60)).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`;}
(async()=>{
 const pieces=[],editMap=[];let position=0,index=0;
 const full=path.join(dir,'qingtian-showcase-full.mp4');
 if(flags['short-only']){
  const prior=JSON.parse(fs.readFileSync(path.join(dir,'media-verification.json')));
  meta.chapters=prior.chapters;editMap.push(...prior.edit_map);
 }else{
 await prepareCrops();
 const ending=meta.chapters.at(-1),previewStart=ending.start+1,previewEnd=previewStart+8;
 pieces.push(await segment(index++,previewStart,previewEnd,'00   先看全景：32 条需求，8 类去向','后段真实画面预览；接下来，从收件开始回放。合成需求 / 本地 fixture，不是生产执行。'));
 editMap.push({output_start:0,output_end:8,source_start:previewStart,source_end:previewEnd,kind:'cold-open-preview'});position=8;
 for(const chapter of meta.chapters){
  chapter.output_start=position;
  const bounds=[chapter.start,chapter.end,...meta.cameras.flatMap(c=>[c.start,c.end,...(c.crop_top!==c.crop_bottom?[(c.start+c.end)/2]:[])])]
    .filter(t=>t>=chapter.start && t<=chapter.end).sort((a,b)=>a-b).filter((t,i,a)=>!i||t-a[i-1]>.02);
  for(let b=0;b<bounds.length-1;b++){
   const start=bounds[b],end=bounds[b+1];if(end-start<.06)continue;
   const mid=(start+end)/2,camera=meta.cameras.find(c=>mid>=c.start&&mid<c.end),crop=cropAt(mid);
   pieces.push(await segment(index++,start,end,chapter.number+'   '+chapter.title,chapter.subtitle,crop,!!camera?.technical));
   const clipDuration=Math.round((end-start)*30)/30;
   editMap.push({output_start:position,output_end:position+clipDuration,source_start:start,source_end:end,chapter:chapter.number,crop});
   position+=clipDuration;
  }
  chapter.output_end=position;
  console.log(JSON.stringify({rendered:chapter.number,seconds:position.toFixed(2)}));
 }
 const list=path.join(work,'full-concat.txt');fs.writeFileSync(list,pieces.map(p=>`file '${p.replaceAll("'","'\\''")}'`).join('\n')+'\n');
 await run(ffmpeg,['-hide_banner','-loglevel','warning','-y','-f','concat','-safe','0','-i',list,'-vf','fps=30','-c:v','libx264','-preset','veryfast','-crf','18','-threads','4','-movflags','+faststart',full]);
 }
 const ranges=[{start:0,length:8,label:'总览'},...[
  ['01',1,6],['03',1,9],['04',3,9],['06',3,9],['07',2,8],['08',2,9],['09',11,9]
 ].map(([n,offset,length])=>({start:meta.chapters.find(c=>c.number===n).output_start+offset,length,label:n})),
 {start:meta.chapters.at(-1).output_end-9,length:9,label:'11'}];
 const shortPieces=[];let shortPosition=0;
 for(let i=0;i<ranges.length;i++){
  const r=ranges[i],file=path.join(work,'short-'+i+'.mp4');
  await run(ffmpeg,['-hide_banner','-loglevel','warning','-y','-ss',r.start.toFixed(3),'-i',full,'-an','-vf','setpts=PTS-STARTPTS,fps=30',
    '-frames:v',String(r.length*30),'-r','30','-c:v','libx264','-preset','veryfast','-crf','19','-threads','4',file]);
  r.short_start=shortPosition;shortPosition+=r.length;shortPieces.push(file);
 }
 const shortList=path.join(work,'short-concat.txt');fs.writeFileSync(shortList,shortPieces.map(p=>`file '${p}'`).join('\n')+'\n');
 const short=path.join(dir,'qingtian-showcase-short.mp4');
 await run(ffmpeg,['-hide_banner','-loglevel','warning','-y','-f','concat','-safe','0','-i',shortList,'-c','copy','-movflags','+faststart',short]);
 const poster=path.join(dir,'poster.png');
 await run(ffmpeg,['-hide_banner','-loglevel','warning','-y','-ss','2','-i',full,'-frames:v','1',poster]);
 // Public documentation gets the unmodified live screenshot with an explicitly editorial banner.
 const board=await loadImage(path.join(dir,'actual-board-complete.png'));
 const canvas=createCanvas(board.width,board.height+80),c=canvas.getContext('2d');
 c.fillStyle='#080b10';c.fillRect(0,0,canvas.width,80);c.fillStyle='#7ce6dd';c.font='30px ShowcaseCN';
 c.fillText('真实引擎 · 合成示例 · 非生产执行  |  32 条需求 · 8 类状态',32,51);c.drawImage(board,0,80);
 const publicBoard=path.join(dir,'actual-board-public.png');fs.writeFileSync(publicBoard,canvas.toBuffer('image/png'));
 const probes={};for(const f of [full,short])probes[path.basename(f)]=JSON.parse(await run(ffprobe,['-v','error','-show_format','-show_streams','-of','json',f]));
 for(const p of Object.values(probes)){
  const v=p.streams.find(s=>s.codec_type==='video');if(v.width!==1920||v.height!==1080||v.codec_name!=='h264'||v.avg_frame_rate!=='30/1'||v.sample_aspect_ratio!=='1:1')throw Error('Media validation failed');
 }
 const finalMap={recording_wall_seconds:meta.raw_end_seconds,playback_speed:1,
  editing:'8-second preview from final board, then chronological chapters; jump cuts and crops only; no fabricated state animation',
  full_duration:Number(probes[path.basename(full)].format.duration),short_duration:Number(probes[path.basename(short)].format.duration),
  chapters:meta.chapters,edit_map:editMap,short_ranges:ranges,source_commit:verify.source_commit,source_sha256:verify.source_sha256,
  outputs:[full,short,poster,publicBoard].map(file=>({file,sha256:sha(file)})),probes};
 fs.writeFileSync(path.join(dir,'media-verification.json'),JSON.stringify(finalMap,null,2));
 fs.writeFileSync(path.join(dir,'chapters.md'),'# 擎天真实引擎合成示例录像\n\n真实引擎 · 合成示例 · 非生产执行。全片实录 1×，片头为后段 8 秒预览；短片为跳切选段，无加速。\n\n'+
  ['00:00 全景预览',...meta.chapters.map(c=>`${timecode(c.output_start)} ${c.title}`)].join('\n\n')+'\n');
 console.log(JSON.stringify({complete:true,full,short,full_seconds:finalMap.full_duration,short_seconds:finalMap.short_duration}));
})().catch(error=>{fs.writeFileSync(path.join(dir,'RENDER-FAILED.txt'),error.stack+'\n');console.error(error);process.exitCode=1;});
