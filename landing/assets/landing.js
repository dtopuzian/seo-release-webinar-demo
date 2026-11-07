"use strict";

const button = document.querySelector("[data-reaction]");
const reaction = document.querySelector(".reaction");
if (button && reaction) {
  button.addEventListener("click", () => {
    reaction.textContent = button.dataset.reaction;
  });
}
