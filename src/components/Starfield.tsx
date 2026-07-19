import { useEffect, useRef } from 'react'

interface Star {
  x: number
  y: number
  z: number
  px: number
  py: number
  size: number
  opacity: number
  speed: number
}

export function Starfield() {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    let animId = 0
    const stars: Star[] = []
    const STAR_COUNT = 280

    const resize = () => {
      canvas.width = window.innerWidth
      canvas.height = window.innerHeight
    }
    resize()
    window.addEventListener('resize', resize)

    // Init stars
    for (let i = 0; i < STAR_COUNT; i++) {
      stars.push({
        x: Math.random() * canvas.width,
        y: Math.random() * canvas.height,
        z: Math.random(),
        px: 0, py: 0,
        size: Math.random() * 1.5 + 0.2,
        opacity: Math.random() * 0.7 + 0.15,
        speed: Math.random() * 0.06 + 0.01,
      })
    }

    const draw = () => {
      ctx.fillStyle = 'rgba(3, 3, 16, 0.18)'
      ctx.fillRect(0, 0, canvas.width, canvas.height)

      for (const star of stars) {
        // Subtle drift upward
        star.y -= star.speed
        if (star.y < -2) {
          star.y = canvas.height + 2
          star.x = Math.random() * canvas.width
        }

        // Twinkle
        const twinkle = Math.sin(Date.now() * 0.001 * star.speed * 8 + star.z * 100) * 0.15

        ctx.beginPath()
        ctx.arc(star.x, star.y, star.size, 0, Math.PI * 2)
        ctx.fillStyle = `rgba(200, 215, 255, ${Math.max(0, star.opacity + twinkle)})`
        ctx.fill()
      }

      // Subtle golden nebula wisps
      const t = Date.now() * 0.0002
      for (let i = 0; i < 3; i++) {
        const gx = canvas.width * (0.2 + i * 0.3 + Math.sin(t + i) * 0.05)
        const gy = canvas.height * (0.3 + Math.cos(t * 0.7 + i) * 0.1)
        const gr = ctx.createRadialGradient(gx, gy, 0, gx, gy, 180 + i * 40)
        gr.addColorStop(0, `rgba(180, 140, 60, 0.025)`)
        gr.addColorStop(1, 'transparent')
        ctx.fillStyle = gr
        ctx.fillRect(0, 0, canvas.width, canvas.height)
      }

      animId = requestAnimationFrame(draw)
    }

    draw()

    return () => {
      cancelAnimationFrame(animId)
      window.removeEventListener('resize', resize)
    }
  }, [])

  return <canvas ref={canvasRef} className="starfield" />
}
