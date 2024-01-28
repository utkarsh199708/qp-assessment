package com.grocery.booking.grocery.Entity;

import jakarta.persistence.*;
import lombok.Data;

@Entity
@Data
public class OrderItem {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne
    @JoinColumn(name = "grocery_item_id")
    private GroceryItem groceryItem;

    private int quantity;

    // getters and setters
}
