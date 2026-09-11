#!/usr/bin/env node
/* Play existing MP4s through a temporary loopback file-only server. */
const fs=require('fs'),path=require('path'),http=require('http'),{chromium}=require('playwright');
const directory=path.resolve(process.argv[2]||''),allowed=['qingtian-showcase-full.mp4','qingtian-showcase-short.mp4'];
const server=http.createServer((req,res)=>{
 const name=req.url.slice(1);if(!allowed.includes(name)){res.writeHead(404);return res.end();}
 const file=path.join(directory,name),stat=fs.statSync(file),match=/bytes=(\d+)-(\d*)/.exec(req.headers.range||'');
 const start=match?Number(match[1]):0,end=match&&match[2]?Number(match[2]):stat.size-1;
 res.writeHead(match?206:200,{'Content-Type':'video/mp4','Accept-Ranges':'bytes','Content-Length':end-start+1,
   ...(match?{'Content-Range':`bytes ${start}-${end}/${stat.size}`}:{})});
 fs.createReadStream(file,{start,end}).pipe(res);
});
let browser;
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const port=server.address().port;
 browser=await chromium.launch({headless:true,...(process.env.SHOWCASE_CHROMIUM?{executablePath:process.env.SHOWCASE_CHROMIUM}:{})});
 const page=await browser.newPage();const results=[];
 for(const name of allowed){
  await page.goto(`http://127.0.0.1:${port}/${name}`,{waitUntil:'domcontentloaded'});
  const result=await page.evaluate(async()=>{
   const v=document.querySelector('video');v.muted=true;await v.play();
   await new Promise((resolve,reject)=>{const start=performance.now();function tick(){if(v.currentTime>.3)return resolve();if(v.error||performance.now()-start>10000)return reject(Error('Video did not play'));setTimeout(tick,40);}tick();});
   v.pause();const first=v.currentTime;v.currentTime=v.duration*.8;
   await new Promise((resolve,reject)=>{v.addEventListener('seeked',resolve,{once:true});setTimeout(()=>reject(Error('Seek timed out')),10000);});
   return {duration:v.duration,width:v.videoWidth,height:v.videoHeight,playback_advanced_to:first,seek_seconds:v.currentTime,error:v.error,ready_state:v.readyState};
  });
  if(result.width!==1920||result.height!==1080||result.error)throw Error('Playback check failed: '+JSON.stringify(result));
  results.push({file:name,...result});
 }
 await browser.close();server.close();
 fs.writeFileSync(path.join(directory,'qa','playback.json'),JSON.stringify({passed:true,results},null,2));
 console.log(JSON.stringify({passed:true,results}));
})().catch(async error=>{console.error(error);if(browser)await browser.close();server.close();process.exitCode=1;});
