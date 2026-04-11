/* esm.sh - @page-agent/page-controller@1.7.1/dist/lib/SimulatorMask-CU7szDjy */
import{Motion as B}from"./ai-motion.mjs";(function(){"use strict";try{if(typeof document<"u"){var t=document.createElement("style");t.appendChild(document.createTextNode(`._wrapper_1ooyb_1 {
	position: fixed;
	inset: 0;
	z-index: 2147483641; /* \u786E\u4FDD\u5728\u6240\u6709\u5143\u7D20\u4E4B\u4E0A\uFF0C\u9664\u4E86 panel */
	cursor: wait;
	overflow: hidden;

	display: none;
}

._wrapper_1ooyb_1._visible_1ooyb_11 {
	display: block;
}
/* AI \u5149\u6807\u6837\u5F0F */
._cursor_1dgwb_2 {
	position: absolute;
	width: var(--cursor-size, 75px);
	height: var(--cursor-size, 75px);
	pointer-events: none;
	z-index: 10000;
}

._cursorBorder_1dgwb_10 {
	position: absolute;
	width: 100%;
	height: 100%;
	background: linear-gradient(45deg, rgb(57, 182, 255), rgb(189, 69, 251));
	mask-image: url("data:image/svg+xml,%3csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%20100%20100'%20fill='none'%3e%3cg%3e%3cpath%20d='M%2015%2042%20L%2015%2036.99%20Q%2015%2031.99%2023.7%2031.99%20L%2028.05%2031.99%20Q%2032.41%2031.99%2032.41%2021.99%20L%2032.41%2017%20Q%2032.41%2012%2041.09%2016.95%20L%2076.31%2037.05%20Q%2085%2042%2076.31%2046.95%20L%2041.09%2067.05%20Q%2032.41%2072%2032.41%2062.01%20L%2032.41%2057.01%20Q%2032.41%2052.01%2023.7%2052.01%20L%2019.35%2052.01%20Q%2015%2052.01%2015%2047.01%20Z'%20fill='none'%20stroke='%23000000'%20stroke-width='6'%20stroke-miterlimit='10'%20style='stroke:%20light-dark(rgb(0,%200,%200),%20rgb(255,%20255,%20255));'/%3e%3c/g%3e%3c/svg%3e");
	mask-size: 100% 100%;
	mask-repeat: no-repeat;

	transform-origin: center;
	transform: rotate(-135deg) scale(1.2);
	margin-left: -10px;
	margin-top: -18px;
}

._cursorFilling_1dgwb_25 {
	position: absolute;
	width: 100%;
	height: 100%;
	background: url("data:image/svg+xml,%3csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%20100%20100'%3e%3cdefs%3e%3c/defs%3e%3cg%20xmlns='http://www.w3.org/2000/svg'%20style='filter:%20drop-shadow(light-dark(rgba(0,%200,%200,%200.4),%20rgba(237,%20237,%20237,%200.4))%203px%204px%204px);'%3e%3cpath%20d='M%2015%2042%20L%2015%2036.99%20Q%2015%2031.99%2023.7%2031.99%20L%2028.05%2031.99%20Q%2032.41%2031.99%2032.41%2021.99%20L%2032.41%2017%20Q%2032.41%2012%2041.09%2016.95%20L%2076.31%2037.05%20Q%2085%2042%2076.31%2046.95%20L%2041.09%2067.05%20Q%2032.41%2072%2032.41%2062.01%20L%2032.41%2057.01%20Q%2032.41%2052.01%2023.7%2052.01%20L%2019.35%2052.01%20Q%2015%2052.01%2015%2047.01%20Z'%20fill='%23ffffff'%20stroke='none'%20style='fill:%20%23ffffff;'/%3e%3c/g%3e%3c/svg%3e");
	background-size: 100% 100%;
	background-repeat: no-repeat;

	transform-origin: center;
	transform: rotate(-135deg) scale(1.2);
	margin-left: -10px;
	margin-top: -18px;
}

._cursorRipple_1dgwb_39 {
	position: absolute;
	width: 100%;
	height: 100%;
	pointer-events: none;
	margin-left: -50%;
	margin-top: -50%;

	&::after {
		content: '';
		opacity: 0;
		position: absolute;
		inset: 0;
		border: 4px solid rgba(57, 182, 255, 1);
		border-radius: 50%;
	}
}

._cursor_1dgwb_2._clicking_1dgwb_57 ._cursorRipple_1dgwb_39::after {
	animation: _cursor-ripple_1dgwb_1 300ms ease-out forwards;
}

@keyframes _cursor-ripple_1dgwb_1 {
	0% {
		transform: scale(0);
		opacity: 1;
	}
	100% {
		transform: scale(2);
		opacity: 0;
	}
}`)),document.head.appendChild(t)}}catch(e){console.error("vite-plugin-css-injected-by-js",e)}})();var y=Object.defineProperty,E=t=>{throw TypeError(t)},S=(t,e,r)=>e in t?y(t,e,{enumerable:!0,configurable:!0,writable:!0,value:r}):t[e]=r,a=(t,e)=>y(t,"name",{value:e,configurable:!0}),f=(t,e,r)=>S(t,typeof e!="symbol"?e+"":e,r),L=(t,e,r)=>e.has(t)||E("Cannot "+r),s=(t,e,r)=>(L(t,e,"read from private field"),r?r.call(t):e.get(t)),g=(t,e,r)=>e.has(t)?E("Cannot add the same private member more than once"):e instanceof WeakSet?e.add(t):e.set(t,r),l=(t,e,r,n)=>(L(t,e,"write to private field"),n?n.call(t,r):e.set(t,r),r),v=(t,e,r)=>(L(t,e,"access private method"),r),o,d,c,h,u,w,C,b;function P(){let t=["dark","dark-mode","theme-dark","night","night-mode"],e=document.documentElement,r=document.body||document.documentElement;for(let m of t)if(e.classList.contains(m)||r?.classList.contains(m))return!0;return!!e.getAttribute("data-theme")?.toLowerCase().includes("dark")}a(P,"hasDarkModeClass");function x(t){let e=/rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(t);return e?{r:parseInt(e[1]),g:parseInt(e[2]),b:parseInt(e[3])}:null}a(x,"parseRgbColor");function k(t,e=128){if(!t||t==="transparent"||t.startsWith("rgba(0, 0, 0, 0)"))return!1;let r=x(t);return r?.299*r.r+.587*r.g+.114*r.b<e:!1}a(k,"isColorDark");function M(){let t=window.getComputedStyle(document.documentElement),e=window.getComputedStyle(document.body||document.documentElement),r=t.backgroundColor,n=e.backgroundColor;return k(n)?!0:n==="transparent"||n.startsWith("rgba(0, 0, 0, 0)")?k(r):!1}a(M,"isBackgroundDark");function T(){try{return!!(P()||M())}catch(t){return console.warn("Error determining if page is dark:",t),!1}}a(T,"isPageDark");var W="_wrapper_1ooyb_1",R="_visible_1ooyb_11",_={wrapper:W,visible:R},N="_cursor_1dgwb_2",z="_cursorBorder_1dgwb_10",F="_cursorFilling_1dgwb_25",I="_cursorRipple_1dgwb_39",$="_clicking_1dgwb_57",p={cursor:N,cursorBorder:z,cursorFilling:F,cursorRipple:I,clicking:$},D=class extends EventTarget{constructor(){super(),g(this,w),f(this,"shown",!1),f(this,"wrapper",document.createElement("div")),f(this,"motion",null),g(this,o,document.createElement("div")),g(this,d,0),g(this,c,0),g(this,h,0),g(this,u,0),this.wrapper.id="page-agent-runtime_simulator-mask",this.wrapper.className=_.wrapper,this.wrapper.setAttribute("data-browser-use-ignore","true"),this.wrapper.setAttribute("data-page-agent-ignore","true");try{let i=new B({mode:T()?"dark":"light",styles:{position:"absolute",inset:"0"}});this.motion=i,this.wrapper.appendChild(i.element),i.autoResize(this.wrapper)}catch(i){console.warn("[SimulatorMask] Motion overlay unavailable:",i)}this.wrapper.addEventListener("click",i=>{i.stopPropagation(),i.preventDefault()}),this.wrapper.addEventListener("mousedown",i=>{i.stopPropagation(),i.preventDefault()}),this.wrapper.addEventListener("mouseup",i=>{i.stopPropagation(),i.preventDefault()}),this.wrapper.addEventListener("mousemove",i=>{i.stopPropagation(),i.preventDefault()}),this.wrapper.addEventListener("wheel",i=>{i.stopPropagation(),i.preventDefault()}),this.wrapper.addEventListener("keydown",i=>{i.stopPropagation(),i.preventDefault()}),this.wrapper.addEventListener("keyup",i=>{i.stopPropagation(),i.preventDefault()}),v(this,w,C).call(this),document.body.appendChild(this.wrapper),v(this,w,b).call(this);let e=a(i=>{let{x:A,y:Q}=i.detail;this.setCursorPosition(A,Q)},"movePointerToListener"),r=a(()=>{this.triggerClickAnimation()},"clickPointerListener"),n=a(()=>{this.wrapper.style.pointerEvents="none"},"enablePassThroughListener"),m=a(()=>{this.wrapper.style.pointerEvents="auto"},"disablePassThroughListener");window.addEventListener("PageAgent::MovePointerTo",e),window.addEventListener("PageAgent::ClickPointer",r),window.addEventListener("PageAgent::EnablePassThrough",n),window.addEventListener("PageAgent::DisablePassThrough",m),this.addEventListener("dispose",()=>{window.removeEventListener("PageAgent::MovePointerTo",e),window.removeEventListener("PageAgent::ClickPointer",r),window.removeEventListener("PageAgent::EnablePassThrough",n),window.removeEventListener("PageAgent::DisablePassThrough",m)})}setCursorPosition(e,r){l(this,h,e),l(this,u,r)}triggerClickAnimation(){s(this,o).classList.remove(p.clicking),s(this,o).offsetHeight,s(this,o).classList.add(p.clicking)}show(){this.shown||(this.shown=!0,this.motion?.start(),this.motion?.fadeIn(),this.wrapper.classList.add(_.visible),l(this,d,window.innerWidth/2),l(this,c,window.innerHeight/2),l(this,h,s(this,d)),l(this,u,s(this,c)),s(this,o).style.left=`${s(this,d)}px`,s(this,o).style.top=`${s(this,c)}px`)}hide(){this.shown&&(this.shown=!1,this.motion?.fadeOut(),this.motion?.pause(),s(this,o).classList.remove(p.clicking),setTimeout(()=>{this.wrapper.classList.remove(_.visible)},800))}dispose(){console.log("dispose SimulatorMask"),this.motion?.dispose(),this.wrapper.remove(),this.dispatchEvent(new Event("dispose"))}};o=new WeakMap;d=new WeakMap;c=new WeakMap;h=new WeakMap;u=new WeakMap;w=new WeakSet;C=a(function(){s(this,o).className=p.cursor;let t=document.createElement("div");t.className=p.cursorRipple,s(this,o).appendChild(t);let e=document.createElement("div");e.className=p.cursorFilling,s(this,o).appendChild(e);let r=document.createElement("div");r.className=p.cursorBorder,s(this,o).appendChild(r),this.wrapper.appendChild(s(this,o))},"#createCursor");b=a(function(){let t=s(this,d)+(s(this,h)-s(this,d))*.2,e=s(this,c)+(s(this,u)-s(this,c))*.2,r=Math.abs(t-s(this,h));r>0&&(r<2?l(this,d,s(this,h)):l(this,d,t),s(this,o).style.left=`${s(this,d)}px`);let n=Math.abs(e-s(this,u));n>0&&(n<2?l(this,c,s(this,u)):l(this,c,e),s(this,o).style.top=`${s(this,c)}px`),requestAnimationFrame(()=>v(this,w,b).call(this))},"#moveCursorToTarget");a(D,"SimulatorMask");var Y=D;export{Y as SimulatorMask};
//# sourceMappingURL=SimulatorMask-CU7szDjy.mjs.map