/* esm.sh - ai-motion@0.4.8 */
function V(i,t,o,r){let e=Math.max(1,Math.min(i,t)),s=Math.min(o,20),R=Math.min(s+r,e),d=Math.min(R,Math.floor(i/2)),l=Math.min(R,Math.floor(t/2)),n=S=>S/i*2-1,h=S=>S/t*2-1,E=0,P=i,L=0,p=t,b=d,T=i-d,I=l,y=t-l,c=n(E),a=n(P),_=h(L),B=h(p),x=n(b),G=n(T),f=h(I),u=h(y),m=0,U=0,v=1,F=1,C=d/i,O=1-d/i,g=l/t,w=1-l/t,M=new Float32Array([c,_,a,_,c,f,c,f,a,_,a,f,c,u,a,u,c,B,c,B,a,u,a,B,c,f,x,f,c,u,c,u,x,f,x,u,G,f,a,f,G,u,G,u,a,f,a,u]),D=new Float32Array([m,U,v,U,m,g,m,g,v,U,v,g,m,w,v,w,m,F,m,F,v,w,v,F,m,g,C,g,m,w,m,w,C,g,C,w,O,g,v,g,O,w,O,w,v,g,v,w]);return{positions:M,uvs:D}}function W(i,t,o){let r=i.createShader(t);if(!r)throw new Error("Failed to create shader");if(i.shaderSource(r,o),i.compileShader(r),!i.getShaderParameter(r,i.COMPILE_STATUS)){let e=i.getShaderInfoLog(r)||"Unknown shader error";throw i.deleteShader(r),new Error(e)}return r}function k(i,t,o){let r=W(i,i.VERTEX_SHADER,t),e=W(i,i.FRAGMENT_SHADER,o),s=i.createProgram();if(!s)throw new Error("Failed to create program");if(i.attachShader(s,r),i.attachShader(s,e),i.linkProgram(s),!i.getProgramParameter(s,i.LINK_STATUS)){let A=i.getProgramInfoLog(s)||"Unknown link error";throw i.deleteProgram(s),i.deleteShader(r),i.deleteShader(e),new Error(A)}return i.deleteShader(r),i.deleteShader(e),s}var z=`#version 300 es
precision lowp float;
in vec2 vUV;
out vec4 outColor;
uniform vec2 uResolution;
uniform float uTime;
uniform float uBorderWidth;
uniform float uGlowWidth;
uniform float uBorderRadius;
uniform vec3 uColors[4];
uniform float uGlowExponent;
uniform float uGlowFactor;
const float PI = 3.14159265359;
const float TWO_PI = 2.0 * PI;
const float HALF_PI = 0.5 * PI;
const vec4 startPositions = vec4(0.0, PI, HALF_PI, 1.5 * PI);
const vec4 speeds = vec4(-1.9, -1.9, -1.5, 2.1);
const vec4 innerRadius = vec4(PI * 0.8, PI * 0.7, PI * 0.3, PI * 0.1);
const vec4 outerRadius = vec4(PI * 1.2, PI * 0.9, PI * 0.6, PI * 0.4);
float random(vec2 st) {
return fract(sin(dot(st.xy, vec2(12.9898, 78.233))) * 43758.5453123);
}
vec2 random2(vec2 st) {
return vec2(random(st), random(st + 1.0));
}
float aaStep(float edge, float d) {
float width = fwidth(d);
return smoothstep(edge - width * 0.5, edge + width * 0.5, d);
}
float aaFract(float x) {
float f = fract(x);
float w = fwidth(x);
float smooth_f = f * (1.0 - smoothstep(1.0 - w, 1.0, f));
return smooth_f;
}
float sdRoundedBox(in vec2 p, in vec2 b, in float r) {
vec2 q = abs(p) - b + r;
return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
}
float getInnerGlow(vec2 p, vec2 b, float radius) {
float dist_x = b.x - abs(p.x);
float dist_y = b.y - abs(p.y);
float glow_x = smoothstep(radius, 0.0, dist_x);
float glow_y = smoothstep(radius, 0.0, dist_y);
return 1.0 - (1.0 - glow_x) * (1.0 - glow_y);
}
float getVignette(vec2 uv) {
vec2 vignetteUv = uv;
vignetteUv = vignetteUv * (1.0 - vignetteUv);
float vignette = vignetteUv.x * vignetteUv.y * 25.0;
vignette = pow(vignette, 0.16);
vignette = 1.0 - vignette;
return vignette;
}
float uvToAngle(vec2 uv) {
vec2 center = vec2(0.5);
vec2 dir = uv - center;
return atan(dir.y, dir.x) + PI;
}
void main() {
vec2 uv = vUV;
vec2 pos = uv * uResolution;
vec2 centeredPos = pos - uResolution * 0.5;
vec2 size = uResolution - uBorderWidth;
vec2 halfSize = size * 0.5;
float dBorderBox = sdRoundedBox(centeredPos, halfSize, uBorderRadius);
float border = aaStep(0.0, dBorderBox);
float glow = getInnerGlow(centeredPos, halfSize, uGlowWidth);
float vignette = getVignette(uv);
glow *= vignette;
float posAngle = uvToAngle(uv);
vec4 lightCenter = mod(startPositions + speeds * uTime, TWO_PI);
vec4 angleDist = abs(posAngle - lightCenter);
vec4 disToLight = min(angleDist, TWO_PI - angleDist) / TWO_PI;
float intensityBorder[4];
intensityBorder[0] = 1.0;
intensityBorder[1] = smoothstep(0.4, 0.0, disToLight.y);
intensityBorder[2] = smoothstep(0.4, 0.0, disToLight.z);
intensityBorder[3] = smoothstep(0.2, 0.0, disToLight.w) * 0.5;
vec3 borderColor = vec3(0.0);
for(int i = 0; i < 4; i++) {
borderColor = mix(borderColor, uColors[i], intensityBorder[i]);
}
borderColor *= 1.1;
borderColor = clamp(borderColor, 0.0, 1.0);
float intensityGlow[4];
intensityGlow[0] = smoothstep(0.9, 0.0, disToLight.x);
intensityGlow[1] = smoothstep(0.7, 0.0, disToLight.y);
intensityGlow[2] = smoothstep(0.4, 0.0, disToLight.z);
intensityGlow[3] = smoothstep(0.1, 0.0, disToLight.w) * 0.7;
vec4 breath = smoothstep(0.0, 1.0, sin(uTime * 1.0 + startPositions * PI) * 0.2 + 0.8);
vec3 glowColor = vec3(0.0);
glowColor += uColors[0] * intensityGlow[0] * breath.x;
glowColor += uColors[1] * intensityGlow[1] * breath.y;
glowColor += uColors[2] * intensityGlow[2] * breath.z;
glowColor += uColors[3] * intensityGlow[3] * breath.w * glow;
glow = pow(glow, uGlowExponent);
glow *= random(pos + uTime) * 0.1 + 1.0;
glowColor *= glow * uGlowFactor;
glowColor = clamp(glowColor, 0.0, 1.0);
vec3 color = mix(glowColor, borderColor + glowColor * 0.2, border);
float alpha = mix(glow, 1.0, border);
outColor = vec4(color, alpha);
}`,Y=`#version 300 es
in vec2 aPosition;
in vec2 aUV;
out vec2 vUV;
void main() {
vUV = aUV;
gl_Position = vec4(aPosition, 0.0, 1.0);
}`;var X=["rgb(57, 182, 255)","rgb(189, 69, 251)","rgb(255, 87, 51)","rgb(255, 214, 0)"];function $(i){let t=i.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);if(!t)throw new Error(`Invalid color format: ${i}`);let[,o,r,e]=t;return[parseInt(o)/255,parseInt(r)/255,parseInt(e)/255]}var N=class{element;canvas;options;running=!1;disposed=!1;startTime=0;lastTime=0;rafId=null;glr;observer;constructor(t={}){this.options={width:t.width??600,height:t.height??600,ratio:t.ratio??window.devicePixelRatio??1,borderWidth:t.borderWidth??8,glowWidth:t.glowWidth??200,borderRadius:t.borderRadius??8,mode:t.mode??"light",...t},this.canvas=document.createElement("canvas"),this.options.classNames&&(this.canvas.className=this.options.classNames),this.options.styles&&Object.assign(this.canvas.style,this.options.styles),this.canvas.style.display="block",this.canvas.style.transformOrigin="center",this.canvas.style.pointerEvents="none",this.element=this.canvas,this.setupGL(),this.options.skipGreeting||this.greet()}start(){if(this.disposed)throw new Error("Motion instance has been disposed.");if(this.running)return;if(!this.glr){console.error("WebGL resources are not initialized.");return}this.running=!0,this.startTime=performance.now(),this.resize(this.options.width??600,this.options.height??600,this.options.ratio),this.glr.gl.viewport(0,0,this.canvas.width,this.canvas.height),this.glr.gl.useProgram(this.glr.program),this.glr.gl.uniform2f(this.glr.uResolution,this.canvas.width,this.canvas.height),this.checkGLError(this.glr.gl,"start: after initial setup");let t=()=>{if(!this.running||!this.glr)return;this.rafId=requestAnimationFrame(t);let o=performance.now();if(o-this.lastTime<1e3/32)return;this.lastTime=o;let e=(o-this.startTime)*.001;this.render(e)};this.rafId=requestAnimationFrame(t)}pause(){if(this.disposed)throw new Error("Motion instance has been disposed.");this.running=!1,this.rafId!==null&&cancelAnimationFrame(this.rafId)}dispose(){if(this.disposed)return;this.disposed=!0,this.running=!1,this.rafId!==null&&cancelAnimationFrame(this.rafId);let{gl:t,vao:o,positionBuffer:r,uvBuffer:e,program:s}=this.glr;o&&t.deleteVertexArray(o),r&&t.deleteBuffer(r),e&&t.deleteBuffer(e),t.deleteProgram(s),this.observer&&this.observer.disconnect(),this.canvas.remove()}resize(t,o,r){if(this.disposed)throw new Error("Motion instance has been disposed.");if(this.options.width=t,this.options.height=o,r&&(this.options.ratio=r),!this.running)return;let{gl:e,program:s,vao:A,positionBuffer:R,uvBuffer:d,uResolution:l}=this.glr,n=r??this.options.ratio??window.devicePixelRatio??1,h=Math.max(1,Math.floor(t*n)),E=Math.max(1,Math.floor(o*n));this.canvas.style.width=`${t}px`,this.canvas.style.height=`${o}px`,(this.canvas.width!==h||this.canvas.height!==E)&&(this.canvas.width=h,this.canvas.height=E),e.viewport(0,0,this.canvas.width,this.canvas.height),this.checkGLError(e,"resize: after viewport setup");let{positions:P,uvs:L}=V(this.canvas.width,this.canvas.height,this.options.borderWidth*n,this.options.glowWidth*n);e.bindVertexArray(A),e.bindBuffer(e.ARRAY_BUFFER,R),e.bufferData(e.ARRAY_BUFFER,P,e.STATIC_DRAW);let p=e.getAttribLocation(s,"aPosition");e.enableVertexAttribArray(p),e.vertexAttribPointer(p,2,e.FLOAT,!1,0,0),this.checkGLError(e,"resize: after position buffer update"),e.bindBuffer(e.ARRAY_BUFFER,d),e.bufferData(e.ARRAY_BUFFER,L,e.STATIC_DRAW);let b=e.getAttribLocation(s,"aUV");e.enableVertexAttribArray(b),e.vertexAttribPointer(b,2,e.FLOAT,!1,0,0),this.checkGLError(e,"resize: after UV buffer update"),e.useProgram(s),e.uniform2f(l,this.canvas.width,this.canvas.height),e.uniform1f(this.glr.uBorderWidth,this.options.borderWidth*n),e.uniform1f(this.glr.uGlowWidth,this.options.glowWidth*n),e.uniform1f(this.glr.uBorderRadius,this.options.borderRadius*n),this.checkGLError(e,"resize: after uniform updates");let T=performance.now();this.lastTime=T;let I=(T-this.startTime)*.001;this.render(I)}autoResize(t){this.observer&&this.observer.disconnect(),this.observer=new ResizeObserver(()=>{let o=t.getBoundingClientRect();this.resize(o.width,o.height)}),this.observer.observe(t)}fadeIn(){if(this.disposed)throw new Error("Motion instance has been disposed.");return new Promise((t,o)=>{let r=this.canvas.animate([{opacity:0,transform:"scale(1.2)"},{opacity:1,transform:"scale(1)"}],{duration:300,easing:"ease-out",fill:"forwards"});r.onfinish=()=>t(),r.oncancel=()=>o("canceled")})}fadeOut(){if(this.disposed)throw new Error("Motion instance has been disposed.");return new Promise((t,o)=>{let r=this.canvas.animate([{opacity:1,transform:"scale(1)"},{opacity:0,transform:"scale(1.2)"}],{duration:300,easing:"ease-in",fill:"forwards"});r.onfinish=()=>t(),r.oncancel=()=>o("canceled")})}checkGLError(t,o){let r=t.getError();if(r!==t.NO_ERROR){for(console.group(`\u{1F534} WebGL Error in ${o}`);r!==t.NO_ERROR;){let e=this.getGLErrorName(t,r);console.error(`${e} (0x${r.toString(16)})`),r=t.getError()}console.groupEnd()}}getGLErrorName(t,o){switch(o){case t.INVALID_ENUM:return"INVALID_ENUM";case t.INVALID_VALUE:return"INVALID_VALUE";case t.INVALID_OPERATION:return"INVALID_OPERATION";case t.INVALID_FRAMEBUFFER_OPERATION:return"INVALID_FRAMEBUFFER_OPERATION";case t.OUT_OF_MEMORY:return"OUT_OF_MEMORY";case t.CONTEXT_LOST_WEBGL:return"CONTEXT_LOST_WEBGL";default:return"UNKNOWN_ERROR"}}setupGL(){let t=this.canvas.getContext("webgl2",{antialias:!1,alpha:!0});if(!t)throw new Error("WebGL2 is required but not available.");let o=k(t,Y,z);this.checkGLError(t,"setupGL: after createProgram");let r=t.createVertexArray();t.bindVertexArray(r),this.checkGLError(t,"setupGL: after VAO creation");let e=this.canvas.width||2,s=this.canvas.height||2,{positions:A,uvs:R}=V(e,s,this.options.borderWidth,this.options.glowWidth),d=t.createBuffer();t.bindBuffer(t.ARRAY_BUFFER,d),t.bufferData(t.ARRAY_BUFFER,A,t.STATIC_DRAW);let l=t.getAttribLocation(o,"aPosition");t.enableVertexAttribArray(l),t.vertexAttribPointer(l,2,t.FLOAT,!1,0,0),this.checkGLError(t,"setupGL: after position buffer setup");let n=t.createBuffer();t.bindBuffer(t.ARRAY_BUFFER,n),t.bufferData(t.ARRAY_BUFFER,R,t.STATIC_DRAW);let h=t.getAttribLocation(o,"aUV");t.enableVertexAttribArray(h),t.vertexAttribPointer(h,2,t.FLOAT,!1,0,0),this.checkGLError(t,"setupGL: after UV buffer setup");let E=t.getUniformLocation(o,"uResolution"),P=t.getUniformLocation(o,"uTime"),L=t.getUniformLocation(o,"uBorderWidth"),p=t.getUniformLocation(o,"uGlowWidth"),b=t.getUniformLocation(o,"uBorderRadius"),T=t.getUniformLocation(o,"uColors"),I=t.getUniformLocation(o,"uGlowExponent"),y=t.getUniformLocation(o,"uGlowFactor");t.useProgram(o),t.uniform1f(L,this.options.borderWidth),t.uniform1f(p,this.options.glowWidth),t.uniform1f(b,this.options.borderRadius),this.options.mode==="dark"?(t.uniform1f(I,2),t.uniform1f(y,1.8)):(t.uniform1f(I,1),t.uniform1f(y,1));let c=(this.options.colors||X).map($);for(let a=0;a<c.length;a++)t.uniform3f(t.getUniformLocation(o,`uColors[${a}]`),...c[a]);this.checkGLError(t,"setupGL: after uniform setup"),t.bindVertexArray(null),t.bindBuffer(t.ARRAY_BUFFER,null),this.glr={gl:t,program:o,vao:r,positionBuffer:d,uvBuffer:n,uResolution:E,uTime:P,uBorderWidth:L,uGlowWidth:p,uBorderRadius:b,uColors:T}}render(t){if(!this.glr)return;let{gl:o,program:r,vao:e,uTime:s}=this.glr;o.useProgram(r),o.bindVertexArray(e),o.uniform1f(s,t),o.disable(o.DEPTH_TEST),o.disable(o.CULL_FACE),o.disable(o.BLEND),o.clearColor(0,0,0,0),o.clear(o.COLOR_BUFFER_BIT),o.drawArrays(o.TRIANGLES,0,24),this.checkGLError(o,"render: after draw call"),o.bindVertexArray(null)}greet(){console.log("%c\u{1F308} ai-motion 0.4.8 \u{1F308}","background: linear-gradient(90deg, #39b6ff, #bd45fb, #ff5733, #ffd600); color: white; text-shadow: 0 0 2px rgba(0, 0, 0, 0.2); font-weight: bold; font-size: 1em; padding: 2px 12px; border-radius: 6px;")}};export{N as Motion};
/*! Bundled license information:

ai-motion/build/Motion.js:
  (**
   * AI Motion - WebGL2 animated border with AI-style glow effects
   *
   * @author Simon<gaomeng1900@gmail.com>
   * @license MIT
   * @repository https://github.com/gaomeng1900/ai-motion
   *)
*/
//# sourceMappingURL=ai-motion.mjs.map