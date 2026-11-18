/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule */
/** @typedef {{id:number,name:string,dataset_id:number,direction:string,window_min:number,severe_levels:number[],segment_size:number,params_version:number,approved:boolean,edges:[string,string][]}} SilencePlan */
/** @typedef {{id:number,plan_id:number,params_version:number,status:string,total_events:number,processed_events:number,total_segments:number,processed_segments:number,cursor_event_id:?string,service_stats:Object[],approval:Object,decisions:Object[]}} SilenceReplay */
export {};
