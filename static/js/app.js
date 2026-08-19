document.addEventListener("DOMContentLoaded", () => {
  const amount = document.getElementById("amount");
  const calc = document.getElementById("calc");
  if (amount && calc && window.rate) {
    const update = () => {
      const n = parseFloat(amount.value || 0);
      calc.textContent = (n * Number(window.rate)).toFixed(2) + " ₪";
    };
    amount.addEventListener("input", update);
    update();
  }
});
