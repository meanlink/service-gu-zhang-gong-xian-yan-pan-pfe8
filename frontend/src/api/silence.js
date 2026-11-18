import {api} from './client.js';
export const silence={
 plans:()=>api('/silence/plans'),
 plan:id=>api('/silence/plans/'+id),
 createPlan:data=>api('/silence/plans',{method:'POST',body:JSON.stringify(data)}),
 updatePlan:(id,data)=>api('/silence/plans/'+id,{method:'PATCH',body:JSON.stringify(data)}),
 startReplay:planId=>api('/silence/plans/'+planId+'/replays',{method:'POST'}),
 advance:replayId=>api('/silence/replays/'+replayId+'/advance',{method:'POST'}),
 runToEnd:replayId=>api('/silence/replays/'+replayId+'/run-to-end',{method:'POST'}),
 replay:replayId=>api('/silence/replays/'+replayId),
 approve:replayId=>api('/silence/replays/'+replayId+'/approve',{method:'POST'}),
 briefs:planId=>api('/silence/plans/'+planId+'/briefs'),
 allBriefs:()=>api('/silence/briefs'),
 createBrief:(planId,data)=>api('/silence/plans/'+planId+'/briefs',{method:'POST',body:JSON.stringify(data)}),
};
