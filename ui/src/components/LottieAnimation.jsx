import { useEffect, useRef } from 'react';
import lottie from 'lottie-web';

export default function LottieAnimation({ animationData, loop = true, autoplay = true, style, className }) {
  const containerRef = useRef(null);
  const animRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current || !animationData) return;

    animRef.current = lottie.loadAnimation({
      container: containerRef.current,
      renderer: 'svg',
      loop,
      autoplay,
      animationData,
    });

    return () => {
      if (animRef.current) {
        animRef.current.destroy();
      }
    };
  }, [animationData, loop, autoplay]);

  return <div ref={containerRef} style={style} className={className} />;
}
