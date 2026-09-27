/**
 * Animation features, loaded after first paint. Components render with the lightweight `m.*`
 * elements inside `LazyMotion`; the full feature set (including layout animations) arrives as a
 * separate chunk instead of weighing down the initial bundle.
 */
export { domMax as default } from "motion/react";
