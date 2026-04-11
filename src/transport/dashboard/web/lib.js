export { h, render, Component } from 'https://esm.sh/preact@10.22.0';
export { useState, useEffect, useRef } from 'https://esm.sh/preact@10.22.0/hooks';
import { h } from 'https://esm.sh/preact@10.22.0';
import htm from 'https://esm.sh/htm@3.1.1';
export const html = htm.bind(h);

// bau-css (inline, ~35 lines unminified)
var d=n=>{let a=0,t=11;for(;a<n.length;)t=101*t+n.charCodeAt(a++)>>>0;return"bau"+t},r=(n,a,t,o)=>{let e=n.createElement("style");e.id=t,e.append(o),(a??n.head).append(e)},m=(n,a)=>n.reduce((t,o,e)=>t+o+(a[e]??""),"");
function bauCss(n){let{document:a}=n?.window??window,t=o=>(e,...l)=>{let c=m(e,l),s=d(c);return!a.getElementById(s)&&r(a,n?.target,s,o(s,c)),s};return{css:t((o,e)=>`.${o} { ${e} }`),keyframes:t((o,e)=>`@keyframes ${o} { ${e} }`),createGlobalStyles:t((o,e)=>e)}}
// Mutable style host — default is document.head; web_agent sets this to a
// Shadow DOM root before importing components to isolate from host-page CSS.
export const stylesHost = {};
export const { css, createGlobalStyles } = bauCss(stylesHost);
