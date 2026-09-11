#!/usr/bin/env node
/* Read-only video validation plus derived QA stills; no engine interaction. */
const fs=require('fs'),path=require('path'),crypto=require('crypto'),{spawn}=require('child_process');
const {createCanvas,loadImage}=require('@napi-rs/canvas');
const dir=path.resolve(process.argv[2]||''),ffmpeg=process.env.FFMPEG||'ffmpeg';
const meta=JSON.parse(fs.readFileSync(path.join(dir,'media-verification.json')));
const out=path.join(dir,'qa');fs.mkdirSync(out,{recursive:true});
function run(args){return new Promise((resolve,reject)=>{
 const p=spawn(ffmpeg,args,{stdio:['ignore','pipe','pipe']});let log='';
 p.stderr.on('data',d=>log+=d);p.on('close',code=>code===0?resolve(log):reject(Error(log)));
});}
const digest=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const stamp=sec=>`${String(Math.floor(sec/60)).padStart(2,'0')}:${String(Math.floor(sec%60)).padStart(2,'0')}`;
(async()=>{
 for(const entry of meta.outputs){if(digest(entry.file)!==entry.sha256)throw Error('Output hash mismatch: '+entry.file);}
 const samples=[];
 for(const [name,duration] of [['full',meta.full_duration],['short',meta.short_duration]]){
  const video=path.join(dir,`qingtian-showcase-${name}.mp4`);
  const log=await run(['-hide_banner','-i',video,'-vf','blackdetect=d=0.08:pic_th=0.95:pix_th=0.02','-an','-f','null','-']);
  fs.writeFileSync(path.join(out,name+'-decode.log'),log);
  if(/black_start:/.test(log)) throw Error('Black segment detected');
  const times=[0.2,...Array.from({length:Math.floor(duration/10)},(_,i)=>(i+1)*10),duration-.15];
  if(name==='full')times.push(...meta.edit_map.filter(e=>e.crop).map(e=>(e.output_start+e.output_end)/2));
  const unique=[...new Set(times.map(t=>t.toFixed(2)))].map(Number).sort((a,b)=>a-b);
  for(const time of unique){
   const file=path.join(out,`${name}-${String(Math.round(time*100)).padStart(5,'0')}.png`);
   await run(['-hide_banner','-loglevel','error','-y','-ss',time.toFixed(3),'-i',video,'-frames:v','1',file]);
   const image=await loadImage(file),canvas=createCanvas(1920,1080),ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);
   const pixels=ctx.getImageData(0,0,1920,1080).data;
   let lit=0;for(let p=0;p<pixels.length;p+=4)if(pixels[p]+pixels[p+1]+pixels[p+2]>120)lit++;
   const share=lit/(1920*1080);if(share<.012)throw Error('Nearly empty sample: '+file);
   samples.push({name,time,file,bright_pixel_share:share,sha256:digest(file)});
  }
 }
 for(let offset=0;offset<samples.length;offset+=12){
  const subset=samples.slice(offset,offset+12),canvas=createCanvas(1920,Math.ceil(subset.length/3)*384),c=canvas.getContext('2d');
  c.fillStyle='#080b10';c.fillRect(0,0,canvas.width,canvas.height);c.font='20px sans-serif';
  for(let n=0;n<subset.length;n++){
   const s=subset[n],x=(n%3)*640,y=Math.floor(n/3)*384;
   c.drawImage(await loadImage(s.file),x,y,640,360);c.fillStyle='#fff';c.fillText(s.name+' '+stamp(s.time),x+10,y+379);
  }
  fs.writeFileSync(path.join(out,`contact-${offset/12+1}.png`),canvas.toBuffer('image/png'));
 }
 fs.writeFileSync(path.join(out,'media-qa.json'),JSON.stringify({passed:true,hashes_match:true,full_decode:true,no_black_segments:true,samples},null,2));
 const recording=JSON.parse(fs.readFileSync(path.join(dir,'recording.json')));
 const verification=JSON.parse(fs.readFileSync(path.join(dir,'verification.json')));
 const events=JSON.parse(fs.readFileSync(path.join(dir,'service-events.json')));
 let previousCursor=0;
 const map=recording.commands.map(command=>{
  const cursor=command.entry.cursor;
  const fullTimes=meta.edit_map.filter(e=>command.raw_seconds>=e.source_start&&command.raw_seconds<e.source_end)
   .map(e=>e.output_start+command.raw_seconds-e.source_start);
  const item={operation:command.request,raw_wall_seconds:command.raw_seconds,
   full_video_seconds:fullTimes,full_timecodes:fullTimes.map(stamp),
   tasks:Object.fromEntries((command.request.tasks||[]).map(alias=>[alias,verification.tasks[alias]])),
   service_event_ids:events.filter(e=>e.id>previousCursor&&e.id<=cursor).map(e=>e.id),
   observed_states:command.entry.states,fixture_results:command.request.op==='fixture'?command.entry.result:undefined};
  previousCursor=cursor;return item;
 });
 fs.writeFileSync(path.join(dir,'timecode-map.json'),JSON.stringify({timing_tolerance_seconds:1,
  note:'wall-clock command acknowledgement mapped to edited source intervals; not frame-exact event arrival',
  raw_sha256:digest(recording.raw),commands:map},null,2));
 console.log(JSON.stringify({passed:true,samples:samples.length,contact_sheets:Math.ceil(samples.length/12),out}));
})().catch(e=>{console.error(e);process.exitCode=1;});
